from __future__ import annotations

import copy
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from review_core import ReviewValidationError, deduplicate_findings, render_review, validate_review
from review_core.snapshot import _refresh_content_fingerprint, collect_changes


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def captured_review(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(repository)),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _git(repository, "config", "user.name", "Review Core Test")
    _git(repository, "config", "user.email", "review-core@example.invalid")
    (repository / "app.py").write_text(
        "def read_record(record_id):\n    return records[record_id]\n",
        encoding="utf-8",
    )
    (repository / "helper.py").write_text("def normalize(value):\n    return value\n", encoding="utf-8")
    (repository / "caller.py").write_text("def endpoint(record_id):\n    return read_record(record_id)\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "initial")

    (repository / "app.py").write_text(
        "def read_record(record_id):\n    return records.get(record_id)\n",
        encoding="utf-8",
    )
    (repository / "helper.py").write_text("def normalize(value):\n    return value.strip()\n", encoding="utf-8")
    manifest = collect_changes(repository, context_paths=["caller.py"])
    result = _result_for(manifest)
    return manifest, result


def _result_for(
    manifest: dict[str, Any],
    *,
    semantic_status: str = "completed",
    static_status: str = "missing",
) -> dict[str, Any]:
    changed = manifest["files"]
    app = next(item for item in changed if item["filename"] == "app.py")
    caller = next(item for item in manifest["context_files"] if item["filename"] == "caller.py")
    evidence = [
        {
            "file": "app.py",
            "side": "after",
            "line_start": 2,
            "line_end": 2,
            "snapshot_fingerprint": app["after"]["fingerprint"],
            "description": "Changed lookup now returns a nullable value.",
        },
        {
            "file": "caller.py",
            "side": "after",
            "line_start": 2,
            "line_end": 2,
            "snapshot_fingerprint": caller["after"]["fingerprint"],
            "description": "The caller assumes the lookup always returns a record.",
        },
    ]
    return {
        "schema_version": "2",
        "input_fingerprint": manifest["content_fingerprint"],
        "rules_version": "rules-test-1",
        "review_status": "completed",
        "findings": [
            {
                "finding_id": "finding-null-lookup",
                "category": "null_contract",
                "severity": "HIGH",
                "root_cause": "The lookup now returns null for a missing record.",
                "trigger": "A request uses an unknown record id.",
                "impact": "The caller dereferences a missing record and fails at runtime.",
                "change_reason": "The change replaced indexed access with dict.get.",
                "suggestion": "Handle the missing record before returning it.",
                "validation_status": "confirmed",
                "evidence": evidence,
                "sources": ["host-agent"],
            }
        ],
        "coverage": [
            {
                "file": item["filename"],
                "semantic_status": semantic_status,
                "static_status": static_status,
                "reason": "",
                "context_gaps": [],
            }
            for item in changed
        ],
        "tool_runs": [],
        "limits": {"max_context_files": 20},
        "measurements": {"host": "test-host", "model": None, "tokens": None, "cost": None},
    }


def test_validates_captured_context_and_optional_missing_static_tool(captured_review) -> None:
    manifest, result = captured_review

    validated = validate_review(result, manifest)
    report = render_review(validated)

    assert validated["review_status"] == "completed"
    assert validated["measurements"]["cost"] is None
    assert "Cost: unknown" in report
    assert "caller.py (after:L2-L2)" in report
    assert "| app.py | completed | missing |" in report
    assert "## Static Candidates" not in report


def test_coverage_must_exactly_cover_selected_files(captured_review) -> None:
    manifest, result = captured_review
    result["coverage"].pop()

    with pytest.raises(ReviewValidationError, match="missing selected file"):
        validate_review(result, manifest)


def test_manifest_source_change_without_new_fingerprint_is_rejected(captured_review) -> None:
    manifest, result = captured_review
    changed = copy.deepcopy(manifest)
    changed["files"][0]["diff"] += "\n# changed after capture"

    with pytest.raises(ReviewValidationError, match="content_fingerprint.*does not match"):
        validate_review(result, changed)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("file", "not-captured.py", "not captured"),
        ("line_end", 200, "exceeds the captured snapshot"),
        ("snapshot_fingerprint", "0" * 64, "does not match the captured"),
    ],
)
def test_evidence_must_match_captured_path_bounds_and_fingerprint(
    captured_review, field: str, value: Any, message: str
) -> None:
    manifest, result = captured_review
    result["findings"][0]["evidence"][0][field] = value

    with pytest.raises(ReviewValidationError, match=message):
        validate_review(result, manifest)


def test_invalid_static_state_is_rejected_but_missing_is_supported(captured_review) -> None:
    manifest, result = captured_review
    result["coverage"][0]["static_status"] = "success-ish"

    with pytest.raises(ReviewValidationError, match="static status"):
        validate_review(result, manifest)

    result["coverage"][0]["static_status"] = "missing"
    assert validate_review(result, manifest)["review_status"] == "completed"


def test_context_gap_or_unavailable_selected_source_cannot_remain_completed(captured_review) -> None:
    manifest, result = captured_review
    result["coverage"][0]["context_gaps"] = ["A related caller could not be captured."]
    validated = validate_review(result, manifest)
    assert validated["coverage"][0]["semantic_status"] == "partial"
    assert validated["review_status"] == "partial"

    unavailable_manifest = copy.deepcopy(manifest)
    app = next(item for item in unavailable_manifest["files"] if item["filename"] == "app.py")
    app["after"]["content"] = None
    app["after"]["unavailable_reason"] = "file_too_large"
    _refresh_content_fingerprint(unavailable_manifest)
    result = _result_for(unavailable_manifest)
    result["findings"] = []
    validated = validate_review(result, unavailable_manifest)
    app_coverage = next(item for item in validated["coverage"] if item["file"] == "app.py")
    assert app_coverage["semantic_status"] == "partial"
    assert "required source snapshot unavailable: after" in app_coverage["reason"]
    assert validated["review_status"] == "partial"


def test_empty_failed_result_stays_failed_and_does_not_claim_clean(captured_review) -> None:
    manifest, result = captured_review
    result["findings"] = []
    result["review_status"] = "failed"
    for coverage in result["coverage"]:
        coverage["semantic_status"] = "failed"

    validated = validate_review(result, manifest)
    report = render_review(validated)

    assert validated["review_status"] == "failed"
    assert "no conclusion can be drawn" in report


@pytest.mark.parametrize(
    ("claimed_status", "coverage_statuses", "expected_status"),
    [
        ("uncovered", ["completed", "completed"], "completed"),
        ("uncovered", ["partial", "completed"], "partial"),
        ("uncovered", ["uncovered", "uncovered"], "uncovered"),
        ("partial", ["completed", "completed"], "partial"),
        ("failed", ["completed", "completed"], "failed"),
    ],
)
def test_review_status_is_consistent_with_coverage(
    captured_review,
    claimed_status: str,
    coverage_statuses: list[str],
    expected_status: str,
) -> None:
    manifest, result = captured_review
    result["findings"] = []
    result["review_status"] = claimed_status
    for coverage, semantic_status in zip(result["coverage"], coverage_statuses):
        coverage["semantic_status"] = semantic_status

    validated = validate_review(result, manifest)
    report = render_review(validated)

    assert validated["review_status"] == expected_status
    if expected_status == "completed":
        assert "No confirmed findings were reported for the selected files." in report
        assert "No selected file received semantic review coverage." not in report
    if expected_status == "uncovered":
        assert "No selected file received semantic review coverage." in report


def test_invalid_result_is_rejected_instead_of_becoming_an_empty_clean_report(captured_review) -> None:
    manifest, result = captured_review
    result["findings"] = None

    with pytest.raises(ReviewValidationError, match="findings.*array"):
        validate_review(result, manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [("tokens", float("inf")), ("cost", float("nan")), ("cost", float("inf"))],
)
def test_non_finite_measurements_are_rejected(captured_review, field: str, value: float) -> None:
    manifest, result = captured_review
    result["measurements"][field] = value

    with pytest.raises(ReviewValidationError, match="finite non-negative"):
        validate_review(result, manifest)


def test_needs_confirmation_is_rendered_outside_formal_findings(captured_review) -> None:
    manifest, result = captured_review
    result["findings"][0]["validation_status"] = "needs_confirmation"

    report = render_review(validate_review(result, manifest))

    assert "## Findings\n\nNo confirmed findings were reported." in report
    assert "## Needs Confirmation" in report
    assert "Candidate 1. [HIGH] null_contract" in report


def test_static_tool_candidates_remain_unconfirmed(captured_review) -> None:
    manifest, result = captured_review
    result["tool_runs"] = [
        {
            "tool": "semgrep",
            "status": "completed",
            "duration_ms": 20,
            "files_scanned": ["app.py"],
            "findings": [
                {
                    "file": "app.py",
                    "line": 2,
                    "rule_id": "possible-null-contract",
                    "severity": "WARNING",
                    "message": "Review this nullable return.",
                }
            ],
        }
    ]

    report = render_review(validate_review(result, manifest))

    assert "## Static Candidates" in report
    assert "not counted as findings" in report
    assert "semgrep/possible-null-contract" in report


def test_finding_dedup_requires_same_root_cause_trigger_and_impact() -> None:
    common_evidence = {
        "file": "app.py",
        "side": "after",
        "line_start": 4,
        "line_end": 4,
        "snapshot_fingerprint": hashlib.sha256(b"source").hexdigest(),
        "description": "Same source line.",
    }
    findings = [
        {
            "finding_id": "permission-check",
            "category": "authorization",
            "root_cause": "Authorization is missing.",
            "trigger": "A guest calls the endpoint.",
            "impact": "Private data is disclosed.",
            "severity": "HIGH",
            "validation_status": "confirmed",
            "evidence": [common_evidence],
            "sources": ["security"],
        },
        {
            "finding_id": "tenant-filter",
            "category": "authorization",
            "root_cause": "The query omits tenant isolation.",
            "trigger": "A user requests another tenant's identifier.",
            "impact": "Another tenant's record is returned.",
            "severity": "HIGH",
            "validation_status": "confirmed",
            "evidence": [common_evidence],
            "sources": ["logic"],
        },
    ]

    assert len(deduplicate_findings(findings)) == 2


def test_same_semantic_issue_merges_evidence_and_source_names() -> None:
    finding = {
        "finding_id": "issue-a",
        "category": "authorization",
        "root_cause": " Authorization is missing. ",
        "trigger": "A guest calls the endpoint.",
        "impact": "Private data is disclosed.",
        "severity": "HIGH",
        "validation_status": "confirmed",
        "evidence": [
            {"file": "app.py", "side": "after", "line_start": 4, "line_end": 4, "snapshot_fingerprint": "a", "description": "entry"}
        ],
        "sources": ["security"],
    }
    second = copy.deepcopy(finding)
    second.update(
        {
            "finding_id": "issue-b",
            "root_cause": "Authorization is missing.",
            "evidence": [
                {"file": "app.py", "side": "after", "line_start": 4, "line_end": 4, "snapshot_fingerprint": "a", "description": "related witness"}
            ],
            "sources": ["logic"],
        }
    )

    merged = deduplicate_findings([finding, second])

    assert len(merged) == 1
    assert merged[0]["sources"] == ["logic", "security"]
    assert len(merged[0]["evidence"]) == 2
    assert merged[0]["merged_finding_ids"] == ["issue-a", "issue-b"]


def test_unconfirmed_duplicate_does_not_downgrade_confirmed_finding() -> None:
    base = {
        "category": "authorization",
        "root_cause": "Authorization is missing.",
        "trigger": "A guest calls the endpoint.",
        "impact": "Private data is disclosed.",
        "severity": "HIGH",
        "evidence": [
            {
                "file": "app.py",
                "side": "after",
                "line_start": 4,
                "line_end": 4,
                "snapshot_fingerprint": "same-snapshot",
                "description": "same evidence",
            }
        ],
    }
    merged = deduplicate_findings(
        [
            {**base, "finding_id": "candidate", "validation_status": "needs_confirmation", "sources": ["host"]},
            {**base, "finding_id": "confirmed", "validation_status": "confirmed", "sources": ["security"]},
        ]
    )

    assert len(merged) == 1
    assert merged[0]["validation_status"] == "confirmed"


def test_dedup_does_not_create_transitive_clusters() -> None:
    base = {
        "category": "logic",
        "root_cause": "The same value is mishandled.",
        "trigger": "A boundary value arrives.",
        "impact": "The response is incorrect.",
        "severity": "MEDIUM",
        "validation_status": "confirmed",
        "sources": ["host"],
    }
    findings = []
    for finding_id, start, end in (("a", 1, 2), ("b", 2, 3), ("c", 3, 4)):
        findings.append(
            {
                **base,
                "finding_id": finding_id,
                "evidence": [
                    {
                        "file": "app.py",
                        "side": "after",
                        "line_start": start,
                        "line_end": end,
                        "snapshot_fingerprint": "same-snapshot",
                        "description": finding_id,
                    }
                ],
            }
        )

    clusters = deduplicate_findings(findings)

    assert [item.get("merged_finding_ids", [item["finding_id"]]) for item in clusters] == [["a", "b"], ["c"]]


def test_local_entrypoint_imports_without_model_or_service_sdks(tmp_path: Path) -> None:
    program = """
import builtins
blocked = {"anthropic", "fastapi", "sqlalchemy", "redis", "langchain", "langgraph"}
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".", 1)[0] in blocked:
        raise AssertionError("forbidden SDK import: " + name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
import tools.local_review
assert tools.local_review.review_diff is not None
"""
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
