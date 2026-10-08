from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from review_core.session import (
    ReviewSessionError,
    add_context,
    create_session,
    finalize_session,
    recheck_session,
    resume_session,
)


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(path)),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _git(path, "config", "user.name", "Review Session Test")
    _git(path, "config", "user.email", "review-session@example.invalid")
    (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    (path / "caller.py").write_text("def read():\n    return value\n", encoding="utf-8")
    _git(path, "add", "--all")
    _git(path, "commit", "-m", "initial")
    return path


def _review_result(manifest: dict[str, object], *, include_finding: bool) -> dict[str, object]:
    files = manifest["files"]
    findings: list[dict[str, object]] = []
    if include_finding:
        item = files[0]
        snapshot = item["after"]
        findings.append(
            {
                "finding_id": "prior-missing-record",
                "category": "null_contract",
                "severity": "HIGH",
                "root_cause": "The changed handler returns a missing record without handling it.",
                "trigger": "A request uses an unknown record identifier.",
                "impact": "The caller can fail while using the absent record.",
                "change_reason": "The changed handler now returns an empty lookup result.",
                "suggestion": "Handle the missing record before returning it.",
                "validation_status": "confirmed",
                "evidence": [
                    {
                        "file": item["filename"],
                        "side": "after",
                        "line_start": 1,
                        "line_end": 1,
                        "snapshot_fingerprint": snapshot["fingerprint"],
                        "description": "The changed code returns the missing record value.",
                    }
                ],
                "sources": ["session-test"],
            }
        )
    return {
        "schema_version": "2",
        "input_fingerprint": manifest["content_fingerprint"],
        "rules_version": "session-test-1",
        "review_status": "completed",
        "findings": findings,
        "coverage": [
            {
                "file": item["filename"],
                "semantic_status": "completed",
                "static_status": "not_run",
                "reason": "",
                "context_gaps": [],
            }
            for item in files
        ],
        "tool_runs": [],
        "limits": {},
        "measurements": {"host": None, "model": None, "tokens": None, "cost": None},
    }


def test_start_creates_outside_snapshot_bound_uncovered_draft(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    session_dir = tmp_path / "sessions" / "first"

    record = create_session(repository, session_directory=session_dir)

    assert record["workflow_status"] == "awaiting_semantic_review"
    assert record["review_coverage_status"] == "uncovered"
    assert record["missing_inputs"] == ["semantic_result"]
    assert not session_dir.is_relative_to(repository)
    draft = json.loads((session_dir / record["references"]["draft_result"]).read_text())
    assert draft["input_fingerprint"] == record["fingerprints"]["final"]
    assert draft["review_status"] == "uncovered"
    assert draft["findings"] == []
    assert {item["semantic_status"] for item in draft["coverage"]} == {"uncovered"}

    with pytest.raises(ReviewSessionError, match="already exists"):
        create_session(repository, session_directory=session_dir)
    with pytest.raises(ReviewSessionError, match="outside the reviewed worktree"):
        create_session(repository, session_directory=repository / ".review-session")
    assert not (repository / ".review-session").exists()


def test_staged_resume_ignores_unstaged_content_but_detects_index_change(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(repository, "add", "app.py")
    (repository / "app.py").write_text("value = 3\n", encoding="utf-8")
    session_dir = tmp_path / "sessions" / "staged"

    record = create_session(repository, "staged", session_directory=session_dir)
    manifest = json.loads((session_dir / record["references"]["manifest"]).read_text())
    app = next(item for item in manifest["files"] if item["filename"] == "app.py")
    assert app["after"]["content"] == "value = 2\n"
    assert resume_session(session_dir)["workflow_status"] == "awaiting_semantic_review"

    (repository / "app.py").write_text("value = 4\n", encoding="utf-8")
    _git(repository, "add", "app.py")
    with pytest.raises(ReviewSessionError, match="selected Git snapshot changed"):
        resume_session(session_dir)
    updated = json.loads((session_dir / "session.json").read_text())
    assert updated["review_coverage_status"] == "uncovered"
    assert updated["references"]["validated_result"] is None
    assert updated["failures"][-1]["step"] == "resume"


def test_context_snapshot_is_versioned_and_resume_checks_context_contents(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    _git(repository, "rm", "caller.py")
    _git(repository, "commit", "-m", "remove caller from selected source")
    (repository / "caller.py").write_text("def read():\n    return value\n", encoding="utf-8")
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    session_dir = tmp_path / "sessions" / "with-context"
    record = create_session(repository, session_directory=session_dir)
    original_fingerprint = record["fingerprints"]["final"]

    record = add_context(session_dir, ["caller.py"])

    assert record["fingerprints"]["initial"] != record["fingerprints"]["final"]
    assert record["fingerprints"]["final"] != original_fingerprint
    assert record["review_coverage_status"] == "uncovered"
    manifest = json.loads((session_dir / record["references"]["manifest"]).read_text())
    assert [item["filename"] for item in manifest["context_files"]] == ["caller.py"]
    assert resume_session(session_dir)["workflow_status"] == "awaiting_semantic_review"

    (repository / "caller.py").write_text("def read():\n    return None\n", encoding="utf-8")
    with pytest.raises(ReviewSessionError, match="captured context changed"):
        resume_session(session_dir)


def test_worktree_identity_separates_linked_worktrees(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    session_dir = tmp_path / "sessions" / "primary"
    create_session(repository, session_directory=session_dir)
    linked = tmp_path / "linked-worktree"
    _git(repository, "worktree", "add", "-b", "other-worktree", str(linked), "HEAD")

    with pytest.raises(ReviewSessionError, match="identity does not match"):
        resume_session(session_dir, repository=linked)

    record = json.loads((session_dir / "session.json").read_text())
    assert record["repository_identity"]["git_common_dir"] == _git(
        linked, "rev-parse", "--git-common-dir"
    )
    assert record["failures"][-1]["step"] == "resume"


def test_branch_session_resumes_its_fixed_head_after_branch_advances(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(repository, "commit", "-am", "branch review target")
    reviewed_head = _git(repository, "rev-parse", "HEAD")
    session_dir = tmp_path / "sessions" / "branch"

    record = create_session(
        repository,
        "branch",
        base=base,
        session_directory=session_dir,
    )
    assert record["scope"]["head"] == reviewed_head

    (repository / "app.py").write_text("value = 3\n", encoding="utf-8")
    _git(repository, "commit", "-am", "advance branch")
    assert _git(repository, "rev-parse", "HEAD") != reviewed_head
    assert resume_session(session_dir)["workflow_status"] == "awaiting_semantic_review"


def test_failed_finalization_stays_uncovered_and_empty_draft_never_completes(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    session_dir = tmp_path / "sessions" / "finalize"
    record = create_session(repository, session_directory=session_dir)
    draft = json.loads((session_dir / record["references"]["draft_result"]).read_text())
    invalid = dict(draft)
    invalid["input_fingerprint"] = "0" * 64

    with pytest.raises(ValueError, match="does not match"):
        finalize_session(session_dir, invalid)
    failed = json.loads((session_dir / "session.json").read_text())
    assert failed["workflow_status"] == "awaiting_semantic_review"
    assert failed["review_coverage_status"] == "uncovered"
    assert failed["references"]["report"] is None
    assert failed["failures"][-1]["step"] == "finalize"

    completed = finalize_session(session_dir, draft)
    assert completed["workflow_status"] == "report_ready"
    assert completed["review_coverage_status"] == "uncovered"
    report = (session_dir / completed["references"]["report"]).read_text()
    assert "**Status:** uncovered" in report
    assert completed["missing_inputs"] == ["semantic_coverage:app.py:uncovered"]


def test_recheck_without_current_semantics_records_missing_input(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    previous_dir = tmp_path / "sessions" / "previous"
    previous = create_session(repository, session_directory=previous_dir)
    previous_draft = json.loads(
        (previous_dir / previous["references"]["draft_result"]).read_text()
    )
    finalize_session(previous_dir, previous_draft)

    current_dir = tmp_path / "sessions" / "current"
    current = create_session(
        repository,
        session_directory=current_dir,
        previous_session=previous_dir,
    )
    current = recheck_session(current_dir)

    assert current["workflow_status"] == "recheck_report_ready"
    assert current["review_coverage_status"] == "uncovered"
    assert current["missing_inputs"] == ["current_semantic_result"]
    recheck = json.loads((current_dir / current["references"]["recheck_result"]).read_text())
    assert recheck["current_input_fingerprint"] == current["fingerprints"]["final"]
    assert recheck["recheck_status"] == "partial"

    current_draft = json.loads(
        (current_dir / current["references"]["draft_result"]).read_text()
    )
    finalize_session(current_dir, current_draft)
    current = recheck_session(current_dir)
    assert current["missing_inputs"] == ["semantic_coverage:app.py:uncovered"]
    recheck = json.loads((current_dir / current["references"]["recheck_result"]).read_text())
    assert recheck["findings"] == []


def test_linked_recheck_uses_previous_attempt_after_source_session_advances(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / "app.py").write_text("value = None\n", encoding="utf-8")
    previous_dir = tmp_path / "sessions" / "previous"
    previous = create_session(repository, session_directory=previous_dir)
    previous_manifest = json.loads(
        (previous_dir / previous["references"]["manifest"]).read_text()
    )
    prior_result = _review_result(previous_manifest, include_finding=True)
    first = finalize_session(previous_dir, prior_result)
    first_result_ref = first["references"]["validated_result"]

    current_dir = tmp_path / "sessions" / "current"
    current = create_session(
        repository,
        session_directory=current_dir,
        previous_session=previous_dir,
    )
    assert current["references"]["previous_session"]["validated_result"] == first_result_ref

    finalize_session(previous_dir, _review_result(previous_manifest, include_finding=False))
    latest_previous = json.loads((previous_dir / "session.json").read_text())
    assert latest_previous["references"]["validated_result"] != first_result_ref

    current_manifest = json.loads((current_dir / current["references"]["manifest"]).read_text())
    current = recheck_session(
        current_dir,
        _review_result(current_manifest, include_finding=False),
    )
    result = json.loads((current_dir / current["references"]["recheck_result"]).read_text())
    assert result["findings"][0]["previous_finding_id"] == "prior-missing-record"
    assert result["findings"][0]["status"] == "unverified"
    assert current["references"]["previous_session"]["validated_result"] == first_result_ref


@pytest.mark.parametrize("artifact", ["manifest", "validated_result", "report"])
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_linked_recheck_fails_when_bound_previous_artifact_is_unavailable_or_changed(
    tmp_path: Path, artifact: str, damage: str
) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / "app.py").write_text("value = None\n", encoding="utf-8")
    previous_dir = tmp_path / "sessions" / "previous"
    previous = create_session(repository, session_directory=previous_dir)
    previous_manifest = json.loads(
        (previous_dir / previous["references"]["manifest"]).read_text()
    )
    finalize_session(
        previous_dir,
        _review_result(previous_manifest, include_finding=True),
    )
    current_dir = tmp_path / "sessions" / "current"
    current = create_session(
        repository,
        session_directory=current_dir,
        previous_session=previous_dir,
    )
    link = current["references"]["previous_session"]
    bound_path = previous_dir / link[artifact]
    if damage == "missing":
        bound_path.unlink()
    else:
        bound_path.write_text("{}\n", encoding="utf-8")

    current_manifest = json.loads((current_dir / current["references"]["manifest"]).read_text())
    with pytest.raises(ReviewSessionError, match=f"linked previous {artifact} artifact"):
        recheck_session(
            current_dir,
            _review_result(current_manifest, include_finding=False),
        )

    failed = json.loads((current_dir / "session.json").read_text())
    assert failed["references"]["recheck_result"] is None
    assert failed["failures"][-1]["step"] == "recheck"


def test_unified_entrypoint_runs_under_python_without_site_packages(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    session_dir = tmp_path / "sessions" / "cli"
    script = Path(__file__).resolve().parents[1] / "skills" / "review-changes" / "scripts" / "review_changes.py"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(script),
            "start",
            "--repo",
            str(repository),
            "--session-dir",
            str(session_dir),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"session: {session_dir}" in result.stdout
    assert json.loads((session_dir / "session.json").read_text())["review_coverage_status"] == "uncovered"
