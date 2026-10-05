from __future__ import annotations

import copy
import hashlib

import pytest

from tests.test_review_core import captured_review
from review_core import ReviewValidationError, recheck_review, render_recheck
from review_core.snapshot import _refresh_content_fingerprint


def _current_manifest_and_result(captured_review):
    previous_manifest, previous_result = captured_review
    current_manifest = copy.deepcopy(previous_manifest)
    app = next(item for item in current_manifest["files"] if item["filename"] == "app.py")
    app["after"]["content"] = "def read_record(record_id):\n    return records.get(record_id)\n\n"
    app["after"]["fingerprint"] = hashlib.sha256(
        app["after"]["content"].encode()
    ).hexdigest()
    _refresh_content_fingerprint(current_manifest)
    return current_manifest, previous_result


def test_recheck_marks_same_finding_persisting_when_id_survives(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest = copy.deepcopy(previous_manifest)
    current_result = copy.deepcopy(previous_result)
    current_result["input_fingerprint"] = current_manifest["content_fingerprint"]

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["recheck_status"] == "completed"
    assert result["findings"][0]["status"] == "persisting"
    assert result["new_findings"] == []


def test_recheck_requires_explicit_resolution_reason(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    current_result = copy.deepcopy(previous_result)
    current_result.update(
        {
            "input_fingerprint": current_manifest["content_fingerprint"],
            "findings": [],
            "recheck_decisions": [
                {
                    "previous_finding_id": "finding-null-lookup",
                    "status": "resolved",
                    "reason": "The caller now handles a missing record before dereferencing it.",
                }
            ],
        }
    )

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)
    report = render_recheck(result)

    assert result["recheck_status"] == "completed"
    assert result["findings"][0]["status"] == "resolved"
    assert "resolved" in report


def test_recheck_without_current_semantic_result_is_unverified(captured_review) -> None:
    previous_manifest, previous_result = captured_review

    result = recheck_review(previous_result, previous_manifest, previous_manifest)

    assert result["recheck_status"] == "partial"
    assert result["findings"][0]["status"] == "unverified"
    assert "No current semantic result" in result["findings"][0]["reason"]


def test_recheck_associates_finding_across_rename_and_new_id(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest = copy.deepcopy(previous_manifest)
    changed = next(item for item in current_manifest["files"] if item["filename"] == "app.py")
    changed["filename"] = "new_app.py"
    changed["previous_filename"] = "app.py"
    changed["status"] = "renamed"
    changed["after"]["path"] = "new_app.py"
    current_manifest["content_fingerprint"] = ""
    _refresh_content_fingerprint(current_manifest)

    current_result = copy.deepcopy(previous_result)
    current_result["input_fingerprint"] = current_manifest["content_fingerprint"]
    current_result["findings"][0]["finding_id"] = "finding-after-rename"
    current_result["findings"][0]["evidence"][0]["file"] = "new_app.py"
    current_result["findings"][0]["evidence"][0]["snapshot_fingerprint"] = changed["after"]["fingerprint"]
    current_result["coverage"] = [
        {**item, "file": "new_app.py" if item["file"] == "app.py" else item["file"]}
        for item in current_result["coverage"]
    ]

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "persisting"
    assert result["findings"][0]["current_finding_id"] == "finding-after-rename"
    assert result["new_findings"] == []


def test_failed_current_review_cannot_confirm_resolution(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    current_result = copy.deepcopy(previous_result)
    current_result.update(
        {
            "input_fingerprint": current_manifest["content_fingerprint"],
            "review_status": "failed",
            "findings": [],
            "recheck_decisions": [
                {
                    "previous_finding_id": "finding-null-lookup",
                    "status": "resolved",
                    "reason": "The caller now handles missing records before dereferencing them.",
                }
            ],
        }
    )
    for coverage in current_result["coverage"]:
        coverage["semantic_status"] = "failed"

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["recheck_status"] == "partial"
    assert result["findings"][0]["status"] == "unverified"
    assert "failed or was uncovered" in result["findings"][0]["reason"]


def test_recheck_rejects_decision_for_unknown_prior_finding(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_result = copy.deepcopy(previous_result)
    current_result["recheck_decisions"] = [
        {
            "previous_finding_id": "typo-in-prior-id",
            "status": "resolved",
            "reason": "The trigger is no longer reachable.",
        }
    ]

    with pytest.raises(ReviewValidationError, match="does not identify a prior finding"):
        recheck_review(previous_result, previous_manifest, previous_manifest, current_result)
