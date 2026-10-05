"""Regression tests for source-pinned local Git snapshots."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from review_core.snapshot import SnapshotChangedError, capture_context, collect_changes
from review_core.validation import validate_review


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def init_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(repository)),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    git(repository, "config", "user.name", "Snapshot Test")
    git(repository, "config", "user.email", "snapshot@example.invalid")
    return repository


def commit_all(repository: Path, message: str) -> str:
    git(repository, "add", "--all")
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


def validate_empty_review(manifest: dict) -> dict:
    result = {
        "schema_version": "2",
        "input_fingerprint": manifest["content_fingerprint"],
        "rules_version": "snapshot-test",
        "review_status": "completed",
        "findings": [],
        "coverage": [
            {
                "file": item["filename"],
                "semantic_status": "completed",
                "static_status": "not_run",
                "reason": "",
                "context_gaps": [],
            }
            for item in manifest["files"]
        ],
        "tool_runs": [],
        "limits": {},
        "measurements": {"host": None, "model": None, "tokens": None, "cost": None},
    }
    return validate_review(result, manifest)


def test_worktree_captures_final_contents_once_after_staged_and_unstaged_edits(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    path = repository / "中文 folder" / "service [v1].py"
    path.parent.mkdir()
    path.write_text("value = 1\n", encoding="utf-8")
    commit_all(repository, "initial")

    path.write_text("value = 2\n", encoding="utf-8")
    git(repository, "add", "--", "中文 folder/service [v1].py")
    path.write_text("value = 3\n", encoding="utf-8")

    manifest = collect_changes(repository)

    assert manifest["schema_version"] == "2"
    assert manifest["scope"] == "worktree"
    assert len(manifest["files"]) == 1
    change = manifest["files"][0]
    assert change["filename"] == "中文 folder/service [v1].py"
    assert change["before"]["content"] == "value = 1\n"
    assert change["after"]["content"] == "value = 3\n"
    assert "+value = 3" in change["diff"]
    assert "+value = 2" not in change["diff"]
    assert len(change["before"]["fingerprint"]) == 64
    assert len(change["after"]["fingerprint"]) == 64
    assert validate_empty_review(manifest)["review_status"] == "completed"


def test_staged_reads_index_blob_and_excludes_worktree_only_edit(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    path = repository / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")
    commit_all(repository, "initial")

    path.write_text("value = 'staged'\n", encoding="utf-8")
    git(repository, "add", "app.py")
    path.write_text("value = 'worktree'\n", encoding="utf-8")

    manifest = collect_changes(repository, scope="staged")

    assert manifest["base"] == git(repository, "rev-parse", "HEAD")
    assert manifest["head"] is None
    assert manifest["head_source"] == "index"
    assert manifest["files"][0]["before"]["content"] == "value = 1\n"
    assert manifest["files"][0]["after"]["content"] == "value = 'staged'\n"
    assert "+value = 'staged'" in manifest["files"][0]["diff"]
    assert "worktree" not in manifest["files"][0]["diff"]


def test_context_does_not_follow_directory_symlink_outside_repository(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    commit_all(repository, "initial")
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.py").write_text("private_value = 'not review input'\n", encoding="utf-8")
    try:
        (repository / "linked").symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    manifest = capture_context(collect_changes(repository), ["linked/private.py"])

    after = manifest["context_files"][0]["after"]
    assert after["content"] is None
    assert after["unavailable_reason"] == "symlink_component"


def test_deleted_file_keeps_full_before_source(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    old_source = "def require_admin(user):\n    if not user.is_admin:\n        raise PermissionError()\n"
    (repository / "auth.py").write_text(old_source, encoding="utf-8")
    commit_all(repository, "initial")
    (repository / "auth.py").unlink()

    change = collect_changes(repository)["files"][0]

    assert change["status"] == "deleted"
    assert change["before"]["path"] == "auth.py"
    assert change["before"]["content"] == old_source
    assert change["after"]["path"] is None
    assert "-def require_admin" in change["diff"]


def test_rename_preserves_both_paths_and_sources(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    original = "def load_user(user_id):\n    return users.get(user_id)\n\n\n# keep this file\n"
    (repository / "old_name.py").write_text(original, encoding="utf-8")
    commit_all(repository, "initial")
    git(repository, "mv", "old_name.py", "new_name.py")
    (repository / "new_name.py").write_text(original + "# updated\n", encoding="utf-8")

    change = collect_changes(repository)["files"][0]

    assert change["status"] == "renamed"
    assert change["filename"] == "new_name.py"
    assert change["previous_filename"] == "old_name.py"
    assert change["before"]["path"] == "old_name.py"
    assert change["after"]["path"] == "new_name.py"
    assert change["before"]["content"] == original
    assert change["after"]["content"] == original + "# updated\n"


def test_untracked_paths_are_reported_and_optionally_snapshotted(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    (repository / "tracked.py").write_text("pass\n", encoding="utf-8")
    commit_all(repository, "initial")
    untracked = repository / "new module.py"
    untracked.write_text("answer = 42\n", encoding="utf-8")

    omitted = collect_changes(repository)
    included = collect_changes(repository, include_untracked=True)

    assert omitted["untracked_files"] == ["new module.py"]
    assert omitted["files"] == []
    assert included["files"][0]["status"] == "untracked"
    assert included["files"][0]["after"]["content"] == "answer = 42\n"
    assert "+answer = 42" in included["files"][0]["diff"]


@pytest.mark.parametrize(
    ("data", "limit", "reason"),
    [(b"\x00binary", 100, "binary_file"), (b"large content", 4, "file_too_large")],
)
def test_unavailable_source_is_covered_with_sha256(
    tmp_path: Path, data: bytes, limit: int, reason: str
) -> None:
    repository = init_repo(tmp_path)
    path = repository / "source.py"
    path.write_bytes(b"before\n")
    commit_all(repository, "initial")
    path.write_bytes(data)

    source = collect_changes(repository, max_file_bytes=limit)["files"][0]["after"]

    assert source["content"] is None
    assert source["unavailable_reason"] == reason
    assert len(source["fingerprint"]) == 64


def test_untracked_text_diff_supports_quoted_git_paths(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    (repository / "tracked.py").write_text("pass\n", encoding="utf-8")
    commit_all(repository, "initial")
    path = repository / "中文 [module].py"
    path.write_text("answer = 42\n", encoding="utf-8")

    manifest = collect_changes(repository, include_untracked=True)

    assert manifest["files"][0]["filename"] == "中文 [module].py"
    assert "diff --git" in manifest["files"][0]["diff"]
    assert "answer = 42" in manifest["files"][0]["diff"]


def test_name_status_and_literal_pathspec_preserve_git_quoted_path(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    quoted_name = 'quoted "module"\tname.py'
    path = repository / quoted_name
    path.write_text("value = 1\n", encoding="utf-8")
    commit_all(repository, "initial")
    path.write_text("value = 2\n", encoding="utf-8")

    change = collect_changes(repository)["files"][0]

    assert change["filename"] == quoted_name
    assert change["before"]["content"] == "value = 1\n"
    assert change["after"]["content"] == "value = 2\n"
    assert '\\"module\\"\\tname.py' in change["diff"]


def test_branch_uses_fixed_commits_and_merge_base_sources(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    (repository / "app.py").write_text("value = 'root'\n", encoding="utf-8")
    root_commit = commit_all(repository, "root")

    (repository / "other.py").write_text("main_only = True\n", encoding="utf-8")
    main_commit = commit_all(repository, "main change")

    git(repository, "checkout", "-b", "feature", root_commit)
    (repository / "app.py").write_text("value = 'feature'\n", encoding="utf-8")
    feature_commit = commit_all(repository, "feature change")
    (repository / "app.py").write_text("value = 'dirty worktree'\n", encoding="utf-8")

    manifest = collect_changes(repository, scope="branch", base="main", head="feature")

    assert manifest["base"] == main_commit
    assert manifest["head"] == feature_commit
    assert manifest["merge_base"] == root_commit
    change = next(item for item in manifest["files"] if item["filename"] == "app.py")
    assert change["before"]["content"] == "value = 'root'\n"
    assert change["after"]["content"] == "value = 'feature'\n"
    assert "dirty worktree" not in change["diff"]


def test_context_uses_same_scope_endpoints_and_updates_content_fingerprint(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    (repository / "changed.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "caller.py").write_text("return changed.value\n", encoding="utf-8")
    commit_all(repository, "initial")

    (repository / "changed.py").write_text("value = 2\n", encoding="utf-8")
    (repository / "caller.py").write_text("return 'worktree-only'\n", encoding="utf-8")
    staged = collect_changes(repository, scope="staged")
    staged_with_context = capture_context(staged, ["caller.py"])

    assert staged_with_context["context_files"][0]["before"]["content"] == "return changed.value\n"
    assert staged_with_context["context_files"][0]["after"]["content"] == "return changed.value\n"
    assert staged_with_context["content_fingerprint"] != staged["content_fingerprint"]
    assert validate_empty_review(staged_with_context)["input_fingerprint"] == staged_with_context["content_fingerprint"]


def test_capture_context_rejects_a_changed_review_range(tmp_path: Path) -> None:
    repository = init_repo(tmp_path)
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "caller.py").write_text("pass\n", encoding="utf-8")
    commit_all(repository, "initial")
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    manifest = collect_changes(repository)
    (repository / "app.py").write_text("value = 3\n", encoding="utf-8")

    with pytest.raises(SnapshotChangedError, match="range changed"):
        capture_context(manifest, ["caller.py"])


def test_snapshot_rejects_changes_between_consistency_passes(tmp_path: Path, monkeypatch) -> None:
    repository = init_repo(tmp_path)
    source = repository / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    commit_all(repository, "initial")
    source.write_text("value = 2\n", encoding="utf-8")

    import review_core.snapshot as snapshots

    capture_once = snapshots._capture_once
    calls = 0

    def change_after_first_pass(*args, **kwargs):
        nonlocal calls
        result = capture_once(*args, **kwargs)
        calls += 1
        if calls == 1:
            source.write_text("value = 3\n", encoding="utf-8")
        return result

    monkeypatch.setattr(snapshots, "_capture_once", change_after_first_pass)

    with pytest.raises(SnapshotChangedError, match="changed during snapshot"):
        collect_changes(repository)
