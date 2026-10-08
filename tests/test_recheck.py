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
    app["after"]["content"] = (
        "def read_record(record_id):\n"
        "    record = records.get(record_id)\n"
        "    if record is None:\n"
        "        return None\n"
        "    return record\n"
    )
    app["after"]["fingerprint"] = hashlib.sha256(
        app["after"]["content"].encode()
    ).hexdigest()
    _refresh_content_fingerprint(current_manifest)
    return current_manifest, previous_result


def _evidence(manifest, path, *, side="after", line=1, description="Reviewed current code."):
    source = next(
        snapshot
        for group in ("files", "context_files")
        for item in manifest.get(group, [])
        for snapshot in (item[side],)
        if snapshot["path"] == path and snapshot["content"] is not None
    )
    line = min(line, len(source["content"].splitlines()))
    return {
        "file": path,
        "side": side,
        "line_start": line,
        "line_end": line,
        "snapshot_fingerprint": source["fingerprint"],
        "description": description,
    }


def _resolved_result(manifest, previous_result, evidence):
    current_result = copy.deepcopy(previous_result)
    current_result.update(
        {
            "input_fingerprint": manifest["content_fingerprint"],
            "findings": [],
            "recheck_decisions": [
                {
                    "previous_finding_id": "finding-null-lookup",
                    "status": "resolved",
                    "reason": "The caller checks the nullable result before dereferencing it.",
                    "evidence": evidence,
                }
            ],
        }
    )
    return current_result


def test_recheck_marks_same_finding_persisting_when_id_survives(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest = copy.deepcopy(previous_manifest)
    current_result = copy.deepcopy(previous_result)
    current_result["input_fingerprint"] = current_manifest["content_fingerprint"]

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["recheck_status"] == "completed"
    assert result["findings"][0]["status"] == "persisting"
    assert result["new_findings"] == []


def test_recheck_resolves_with_changed_evidence_and_reviews_prior_context(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [
            _evidence(current_manifest, "app.py", line=3, description="The missing-record path now returns safely."),
            _evidence(current_manifest, "caller.py", line=2, description="The prior caller context was rechecked."),
        ],
    )

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)
    report = render_recheck(result)

    assert result["recheck_status"] == "completed"
    assert result["findings"][0]["status"] == "resolved"
    assert "resolved" in report


def test_resolved_decision_without_evidence_is_unverified(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_result = _resolved_result(previous_manifest, previous_result, [])

    result = recheck_review(previous_result, previous_manifest, previous_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "missing or malformed" in result["findings"][0]["reason"]


@pytest.mark.parametrize("invalid_field", ["description", "line_end"])
def test_malformed_resolution_evidence_is_unverified(captured_review, invalid_field) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    evidence = [
        _evidence(current_manifest, "app.py", line=3),
        _evidence(current_manifest, "caller.py", line=2),
    ]
    if invalid_field == "description":
        evidence[0]["description"] = "  "
    else:
        evidence[0]["line_end"] = 10_000
    current_result = _resolved_result(current_manifest, previous_result, evidence)

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "missing or malformed" in result["findings"][0]["reason"]


def test_before_only_resolution_evidence_is_unverified(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    prior_without_context = copy.deepcopy(previous_result)
    prior_without_context["findings"][0]["evidence"] = [
        _evidence(previous_manifest, "app.py")
    ]
    current_result = _resolved_result(
        current_manifest,
        prior_without_context,
        [_evidence(current_manifest, "app.py", side="before")],
    )

    result = recheck_review(prior_without_context, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "does not show reinspection of prior path 'app.py'" in result["findings"][0]["reason"]


def test_unrelated_changed_helper_does_not_prove_issue_resolution(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest = copy.deepcopy(previous_manifest)
    helper = next(item for item in current_manifest["files"] if item["filename"] == "helper.py")
    helper["after"]["content"] = "def normalize(value):\n    return value.strip().lower()\n"
    helper["after"]["fingerprint"] = hashlib.sha256(helper["after"]["content"].encode()).hexdigest()
    _refresh_content_fingerprint(current_manifest)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [
            _evidence(current_manifest, "helper.py", line=2),
            _evidence(current_manifest, "app.py", line=2),
            _evidence(current_manifest, "caller.py", line=2),
        ],
    )

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "detectable captured source change" in result["findings"][0]["reason"]


def test_unchanged_snapshot_with_valid_evidence_does_not_prove_resolution(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_result = _resolved_result(
        previous_manifest,
        previous_result,
        [
            _evidence(previous_manifest, "app.py", line=2),
            _evidence(previous_manifest, "caller.py", line=2),
        ],
    )

    result = recheck_review(previous_result, previous_manifest, previous_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "detectable captured source change" in result["findings"][0]["reason"]


def test_unrelated_incomplete_file_does_not_block_resolution(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [
            _evidence(current_manifest, "app.py", line=3),
            _evidence(current_manifest, "caller.py", line=2),
        ],
    )
    helper_coverage = next(item for item in current_result["coverage"] if item["file"] == "helper.py")
    helper_coverage["semantic_status"] = "uncovered"
    helper_coverage["reason"] = "Not part of this finding's call path."
    current_result["review_status"] = "partial"

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["recheck_status"] == "partial"
    assert result["findings"][0]["status"] == "resolved"
    assert any(gap["file"] == "helper.py" for gap in result["coverage_gaps"])


def test_incomplete_related_file_coverage_blocks_resolution(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [
            _evidence(current_manifest, "app.py", line=3),
            _evidence(current_manifest, "caller.py", line=2),
        ],
    )
    app_coverage = next(item for item in current_result["coverage"] if item["file"] == "app.py")
    app_coverage["semantic_status"] = "failed"
    app_coverage["reason"] = "The relevant source could not be reviewed."
    current_result["review_status"] = "partial"

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "Related selected file 'app.py' has failed" in result["findings"][0]["reason"]


def test_unavailable_prior_context_blocks_resolution(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    caller = next(item for item in current_manifest["context_files"] if item["filename"] == "caller.py")
    caller["after"].update(
        {"content": None, "fingerprint": None, "unavailable_reason": "context_not_captured"}
    )
    _refresh_content_fingerprint(current_manifest)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [_evidence(current_manifest, "app.py", line=3)],
    )

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "Prior evidence path 'caller.py' has no accessible current after snapshot" in result["findings"][0]["reason"]


@pytest.mark.parametrize(
    ("validation_status", "expected"),
    [("confirmed", "confirmed matching finding"), ("needs_confirmation", "needs confirmation")],
)
def test_resolved_decision_cannot_override_current_matching_finding(
    captured_review, validation_status, expected
) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [
            _evidence(current_manifest, "app.py", line=3),
            _evidence(current_manifest, "caller.py", line=2),
        ],
    )
    current_finding = copy.deepcopy(previous_result["findings"][0])
    current_finding["evidence"] = [
        _evidence(current_manifest, "app.py", line=3),
        _evidence(current_manifest, "caller.py", line=2),
    ]
    current_finding["validation_status"] = validation_status
    current_result["findings"] = [current_finding]

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert expected in result["findings"][0]["reason"]


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


def test_stable_id_match_is_reserved_before_fallback(captured_review) -> None:
    manifest, original_result = captured_review
    previous_result = copy.deepcopy(original_result)
    fallback_prior = copy.deepcopy(previous_result["findings"][0])
    fallback_prior["finding_id"] = "legacy-finding"
    stable_prior = copy.deepcopy(fallback_prior)
    stable_prior.update(
        {
            "finding_id": "stable-finding",
            "category": "different_prior_category",
            "root_cause": "A distinct prior problem.",
            "trigger": "A separate trigger.",
            "impact": "A separate impact.",
        }
    )
    previous_result["findings"] = [fallback_prior, stable_prior]

    current_result = copy.deepcopy(original_result)
    stable_current = copy.deepcopy(fallback_prior)
    stable_current["finding_id"] = "stable-finding"
    current_result["findings"] = [stable_current]

    result = recheck_review(previous_result, manifest, manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert result["findings"][0]["current_finding_id"] is None
    assert result["findings"][1]["current_finding_id"] == "stable-finding"
    assert result["findings"][1]["status"] == "persisting"
    assert result["new_findings"] == []


def test_ambiguous_fallback_association_keeps_all_prior_findings_unverified(captured_review) -> None:
    manifest, original_result = captured_review
    previous_result = copy.deepcopy(original_result)
    first = copy.deepcopy(previous_result["findings"][0])
    first["finding_id"] = "old-first"
    second = copy.deepcopy(first)
    second["finding_id"] = "old-second"
    previous_result["findings"] = [first, second]

    current_result = copy.deepcopy(original_result)
    current_finding = copy.deepcopy(first)
    current_finding["finding_id"] = "new-current"
    current_result["findings"] = [current_finding]

    result = recheck_review(previous_result, manifest, manifest, current_result)

    assert [item["status"] for item in result["findings"]] == ["unverified", "unverified"]
    assert all("ambiguous" in item["reason"] for item in result["findings"]), [
        item["reason"] for item in result["findings"]
    ]
    assert result["new_findings"] == ["new-current"]


def test_resolution_follows_transitive_rename_aliases(captured_review) -> None:
    original_manifest, original_result = captured_review
    previous_manifest = copy.deepcopy(original_manifest)
    prior_file = next(item for item in previous_manifest["files"] if item["filename"] == "app.py")
    prior_file["filename"] = "intermediate.py"
    prior_file["previous_filename"] = "app.py"
    prior_file["status"] = "renamed"
    prior_file["after"]["path"] = "intermediate.py"
    _refresh_content_fingerprint(previous_manifest)

    previous_result = copy.deepcopy(original_result)
    previous_result["input_fingerprint"] = previous_manifest["content_fingerprint"]
    previous_result["findings"][0]["evidence"][0]["file"] = "intermediate.py"
    previous_result["coverage"] = [
        {**item, "file": "intermediate.py" if item["file"] == "app.py" else item["file"]}
        for item in previous_result["coverage"]
    ]

    current_manifest = copy.deepcopy(previous_manifest)
    current_file = next(item for item in current_manifest["files"] if item["filename"] == "intermediate.py")
    current_file["filename"] = "final.py"
    current_file["previous_filename"] = "intermediate.py"
    current_file["status"] = "renamed"
    current_file["before"] = copy.deepcopy(current_file["after"])
    current_file["before"]["path"] = "intermediate.py"
    current_file["after"]["path"] = "final.py"
    current_file["after"]["content"] = (
        "def read_record(record_id):\n"
        "    record = records.get(record_id)\n"
        "    if record is None:\n"
        "        return None\n"
        "    return record\n"
    )
    current_file["after"]["fingerprint"] = hashlib.sha256(
        current_file["after"]["content"].encode()
    ).hexdigest()
    _refresh_content_fingerprint(current_manifest)

    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [
            _evidence(current_manifest, "final.py", line=3),
            _evidence(current_manifest, "caller.py", line=2),
        ],
    )
    current_result["coverage"] = [
        {**item, "file": "final.py" if item["file"] == "intermediate.py" else item["file"]}
        for item in current_result["coverage"]
    ]

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "resolved"


def test_removed_file_resolution_requires_before_and_current_caller_evidence(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest = copy.deepcopy(previous_manifest)
    app = next(item for item in current_manifest["files"] if item["filename"] == "app.py")
    app["status"] = "removed"
    app["diff"] = "--- a/app.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n"
    app["after"] = {
        "path": None,
        "content": None,
        "fingerprint": None,
        "unavailable_reason": None,
    }
    _refresh_content_fingerprint(current_manifest)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [
            _evidence(current_manifest, "app.py", side="before", line=2),
            _evidence(current_manifest, "caller.py", line=2),
        ],
    )

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "resolved"


def test_removed_file_without_current_caller_evidence_is_unverified(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest = copy.deepcopy(previous_manifest)
    app = next(item for item in current_manifest["files"] if item["filename"] == "app.py")
    app["status"] = "removed"
    app["diff"] = "--- a/app.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n"
    app["after"] = {
        "path": None,
        "content": None,
        "fingerprint": None,
        "unavailable_reason": None,
    }
    _refresh_content_fingerprint(current_manifest)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [_evidence(current_manifest, "app.py", side="before", line=2)],
    )

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "also requires current after-side caller or replacement evidence" in result["findings"][0]["reason"]


def test_each_removed_prior_path_requires_its_own_before_evidence(captured_review) -> None:
    previous_manifest, original_result = captured_review
    previous_result = copy.deepcopy(original_result)
    previous_result["findings"][0]["evidence"].append(
        _evidence(previous_manifest, "helper.py", line=2)
    )
    current_manifest = copy.deepcopy(previous_manifest)
    for path in ("app.py", "helper.py"):
        item = next(file for file in current_manifest["files"] if file["filename"] == path)
        item["status"] = "removed"
        item["diff"] = f"--- a/{path}\n+++ /dev/null\n@@ -1,2 +0,0 @@\n"
        item["after"] = {
            "path": None,
            "content": None,
            "fingerprint": None,
            "unavailable_reason": None,
        }
    _refresh_content_fingerprint(current_manifest)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [
            _evidence(current_manifest, "app.py", side="before", line=2),
            _evidence(current_manifest, "caller.py", line=2),
        ],
    )

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "must cite deleted prior path 'helper.py' on its before side" in result["findings"][0]["reason"]


def test_prior_selected_file_moved_to_context_still_requires_after_citation(captured_review) -> None:
    previous_manifest, previous_result = captured_review
    current_manifest, _ = _current_manifest_and_result(captured_review)
    app = next(item for item in current_manifest["files"] if item["filename"] == "app.py")
    app_after = copy.deepcopy(app["after"])
    current_manifest["files"].remove(app)
    current_manifest["context_files"].append(
        {"filename": "app.py", "before": copy.deepcopy(app_after), "after": app_after}
    )
    _refresh_content_fingerprint(current_manifest)
    current_result = _resolved_result(
        current_manifest,
        previous_result,
        [_evidence(current_manifest, "caller.py", line=2)],
    )
    current_result["coverage"] = [
        item for item in current_result["coverage"] if item["file"] != "app.py"
    ]

    result = recheck_review(previous_result, previous_manifest, current_manifest, current_result)

    assert result["findings"][0]["status"] == "unverified"
    assert "does not show reinspection of prior path 'app.py'" in result["findings"][0]["reason"]
