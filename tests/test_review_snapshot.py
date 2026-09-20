"""Regression tests for immutable PR snapshots and Git patch edge cases."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import subprocess

import pytest

from agents.base import FileDiff
from tools.github_client import GitHubClient, _parse_patch
from tools.review_version import review_cache_key


@pytest.fixture
def snapshot():
    repo = MagicMock()
    repo.compare.return_value.merge_base_commit.sha = "ancestor"
    repo.get_contents.return_value = SimpleNamespace(type="file", size=10, decoded_content=b"x = 2\n")
    file = SimpleNamespace(
        filename="app.py", previous_filename=None, status="modified",
        additions=1, deletions=1, patch="@@ -1 +1 @@\n-x = 1\n+x = 2",
    )
    pr = MagicMock()
    pr.base.sha, pr.head.sha = "base", "head"
    pr.base.repo = repo
    pr.changed_files = 1
    pr.get_files.return_value = [file]
    pr.title, pr.body, pr.user.login = "Test", "", "test-user"
    client = GitHubClient(token="test")
    client._get_pr = MagicMock(return_value=pr)
    return client, pr, repo, file


def test_source_is_fetched_at_reviewed_head(snapshot):
    client, pr, repo, _ = snapshot
    result = client.get_pr_diff("https://github.com/owner/repo/pull/1")
    assert (result.base_sha, result.head_sha, result.merge_base_sha) == ("base", "head", "ancestor")
    assert result.files[0].full_source == "x = 2\n"
    repo.get_contents.assert_called_once_with("app.py", ref="head")


@pytest.mark.parametrize("revision", ["base", "head"])
def test_push_during_fetch_rejects_mixed_snapshot(snapshot, revision):
    client, pr, _, _ = snapshot
    changed = MagicMock()
    changed.base.sha, changed.head.sha = "base", "head"
    getattr(changed, revision).sha = "new-revision"
    client._get_pr.side_effect = [pr, changed]
    with pytest.raises(ValueError, match="PR changed"):
        client.get_pr_diff("https://github.com/owner/repo/pull/1")


@pytest.mark.parametrize("patch_text", [None, "@@ -1 +1 @@\n-x = 1"])
def test_missing_or_truncated_patch_uses_merge_base_source(snapshot, patch_text):
    client, _, repo, file = snapshot
    file.patch = patch_text
    file.status, file.previous_filename = "renamed", "old.py"
    repo.get_contents.side_effect = [
        SimpleNamespace(type="file", size=6, decoded_content=b"x = 2\n"),
        SimpleNamespace(type="file", size=6, decoded_content=b"x = 1\n"),
    ]
    result = client.get_pr_diff("https://github.com/owner/repo/pull/1")
    assert result.files[0].added_lines == [(1, "x = 2")]
    assert result.files[0].removed_lines == [(1, "x = 1")]
    repo.get_contents.assert_called_with("old.py", ref="ancestor")


def test_deleted_file_is_explicitly_skipped(snapshot):
    client, _, repo, file = snapshot
    file.status = "removed"
    result = client.get_pr_diff("https://github.com/owner/repo/pull/1")
    assert not result.files
    assert "deleted" in result.skipped_files["app.py"]
    repo.get_contents.assert_not_called()


def test_binary_file_is_explicitly_skipped(snapshot):
    client, _, repo, _ = snapshot
    repo.get_contents.return_value.decoded_content = b"\x00binary"
    result = client.get_pr_diff("https://github.com/owner/repo/pull/1")
    assert not result.files
    assert "binary" in result.skipped_files["app.py"]


def test_incomplete_file_listing_fails(snapshot):
    client, pr, _, _ = snapshot
    pr.changed_files = 2
    with pytest.raises(ValueError, match="incomplete"):
        client.get_pr_diff("https://github.com/owner/repo/pull/1")


def test_patch_markers_preserve_old_and_new_line_numbers():
    added, removed = _parse_patch(
        "@@ -3,2 +3,2 @@\n---old\n\\ No newline at end of file\n"
        "+++new\n unchanged\n@@ -20 +20 @@\n-old\n+new"
    )
    assert added == [(3, "++new"), (20, "new")]
    assert removed == [(3, "--old"), (20, "old")]


def test_parser_accepts_real_git_diff_with_headers_and_no_final_newline(tmp_path):
    old, new = tmp_path / "old.py", tmp_path / "new.py"
    old.write_text("--old\nunchanged\nlast", encoding="utf-8")
    new.write_text("++new\nunchanged\nlast changed", encoding="utf-8")
    diff = subprocess.run(
        ["git", "diff", "--no-index", "--no-ext-diff", "--", str(old), str(new)],
        capture_output=True, text=True, check=False,
    )
    assert diff.returncode == 1
    added, removed = _parse_patch(diff.stdout)
    assert added == [(1, "++new"), (3, "last changed")]
    assert removed == [(1, "--old"), (3, "last")]


def test_inline_review_is_pinned_and_filters_non_diff_lines(snapshot):
    client, pr, repo, _ = snapshot
    findings = [dict(file="app.py", line_start=line, severity="HIGH", category="logic",
                     description="bad", suggestion="fix", confidence=0.9) for line in (1, 50)]
    assert client.post_inline_review(
        "url", findings, commit_sha="head",
        file_diffs=[FileDiff(filename="app.py", added_lines=[(1, "x = 2")])],
    )
    repo.get_commit.assert_called_once_with("head")
    comments = pr.create_review.call_args.kwargs["comments"]
    assert len(comments) == 1
    assert comments[0]["line"] == 1 and comments[0]["side"] == "RIGHT"


def test_stale_inline_review_is_not_published(snapshot):
    client, pr, _, _ = snapshot
    assert not client.post_inline_review("url", [], commit_sha="old", file_diffs=[])
    pr.create_review.assert_not_called()


def test_cache_changes_with_base_head_rules_and_implementation():
    original = review_cache_key("base", "head", "ancestor", "1")
    assert original != review_cache_key("base2", "head", "ancestor", "1")
    assert original != review_cache_key("base", "head2", "ancestor", "1")
    assert original != review_cache_key("base", "head", "ancestor2", "1")
    assert original != review_cache_key("base", "head", "ancestor", "2")
    with patch("tools.review_version.analysis_version", return_value="changed-prompt-or-model"):
        assert original != review_cache_key("base", "head", "ancestor", "1")
