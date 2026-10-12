#!/usr/bin/env python3
"""Export and verify a privacy-limited projection of a sealed workflow batch."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


VERSION = "1.0.0"
EXPORT_ID = "2026-10-12-ab-workflow-v2"
V3_EXPORT_ID = "2026-10-12-ab-workflow-validation-v3"
V3_BENCHMARK_ID = "2026-10-12-ab-workflow-validation-v3"
V3_EXPORTER_VERSION = "2.0.0"
CLOSURE_EXPORTER_VERSION = "2.1.0"
EXPECTED_SOURCE_SHA256 = {
    "seal_report": "d4b906458da7d58b52472beed0b4eca53bb4d45bcb6965eac26580e8523faba3",
    "ab_summary": "27fff54bb1527104c043ad00b99c0c2eebadef4b2dc1e3d7a22b14ea444b6654",
}
CONDITION_NAMES = {"A": "same_host_direct", "B": "current_skill"}
HEX_DIGITS = frozenset("0123456789abcdef")
V3_SOURCE_NAMES = {
    "seal_report": "workflow-report-v3.json",
    "ab_summary": "ab-summary-final-v3.json",
    "dependency_snapshot": "dependencies-before-v3.json",
}
V3_SLOT_ORDER = (
    ("case0002", "A", 1), ("case0003", "B", 1), ("case0006", "A", 1),
    ("case0003", "A", 1), ("case0002", "B", 1), ("case0006", "B", 1),
    ("case0002", "B", 2), ("case0003", "A", 2), ("case0006", "B", 2),
    ("case0003", "B", 2), ("case0002", "A", 2), ("case0006", "A", 2),
)
V3_ELIGIBLE_PAIR_STATUS = "paired_inputs_and_host_identity_verified"


def _profile(export_id: str) -> dict[str, Any]:
    if export_id == EXPORT_ID:
        return {
            "benchmark_id": "2026-10-10-ab-workflow-validation",
            "version": VERSION,
            "source_names": {
                "seal_report": "workflow-report-v2.json",
                "ab_summary": "ab-summary-final-v2.json",
                "dependency_snapshot": "dependencies-before-v2.json",
            },
        }
    if export_id == V3_EXPORT_ID:
        return {
            "benchmark_id": V3_BENCHMARK_ID,
            "version": V3_EXPORTER_VERSION,
            "source_names": V3_SOURCE_NAMES,
        }
    raise ExportError(f"unsupported export id: {export_id}")


def _source_hash_policy(
    export_id: str,
    expected_source_hashes: dict[str, str] | None,
) -> dict[str, str]:
    if expected_source_hashes is not None:
        return expected_source_hashes
    return EXPECTED_SOURCE_SHA256 if export_id == EXPORT_ID else {}


class ExportError(ValueError):
    """Raised when a workflow export cannot be reproduced from its sources."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExportError(f"{label} is not valid UTF-8 JSON") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExportError(message)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in HEX_DIGITS for char in value)
    )


def _validate_v3_plan(
    report: dict[str, Any],
    plan: Any,
    dependency_snapshot: dict[str, Any],
    hashes: dict[str, str],
    benchmark_id: str = V3_BENCHMARK_ID,
) -> list[dict[str, Any]]:
    _require(isinstance(plan, dict), "v3 batch plan must be an object")
    _require(plan.get("schema_version") == 1, "v3 batch plan schema is unsupported")
    _require(plan.get("benchmark_id") == benchmark_id, "v3 batch plan id differs from the closure")
    _require(plan.get("plan_state") == "go", "v3 batch plan is not in GO state")
    _require(plan.get("execution_authorized") is True, "v3 batch plan execution is not authorized")
    _require(plan.get("quality_metrics_separate") is True, "v3 plan does not separate quality metrics")
    _require(plan.get("purpose") == "workflow_reproducibility_validation_of_existing_corpus", "v3 plan purpose is unsupported")
    _require(plan.get("fresh_blind_test") is False, "v3 plan must not claim a fresh blind test")
    _require(plan.get("annotation_status") == "pending", "v3 plan annotations must remain pending")
    _require(report.get("benchmark_id") == benchmark_id, "v3 seal report id differs from the closure")
    _require(dependency_snapshot.get("benchmark_id") == benchmark_id, "v3 dependency snapshot id differs from the closure")
    _require(report.get("source_commit") == dependency_snapshot.get("source_commit"), "v3 seal source commit differs from snapshot")
    _require(
        report.get("immutable_plan_sha256") == dependency_snapshot.get("immutable_plan_sha256"),
        "v3 immutable plan digest differs from pre-execution snapshot",
    )
    _require(_is_sha256(report.get("immutable_plan_sha256")), "v3 immutable plan digest is invalid")
    dependencies = report.get("dependencies")
    _require(isinstance(dependencies, dict), "v3 seal dependency state is missing")
    _require(dependencies.get("source_commit_changed") is False, "v3 seal records a changed source commit")

    plan_slots = plan.get("slots")
    _require(isinstance(plan_slots, list) and len(plan_slots) == 12, "v3 batch plan must contain 12 slots")
    cases = plan.get("cases")
    _require(isinstance(cases, dict) and set(cases) == {"case0002", "case0003", "case0006"}, "v3 batch plan cases are unsupported")
    conditions = plan.get("conditions")
    _require(isinstance(conditions, dict), "v3 batch plan conditions are missing")
    _require(conditions.get("A") == CONDITION_NAMES["A"], "v3 A condition is unsupported")
    _require(
        conditions.get("B") == "same_host_direct_plus_pinned_unreleased_skill_source_snapshot",
        "v3 B condition is unsupported",
    )
    expected_slots: list[dict[str, Any]] = []
    identities: set[tuple[str, str, int]] = set()
    for order, item in enumerate(plan_slots, start=1):
        _require(isinstance(item, dict), "v3 plan slot must be an object")
        case_id = item.get("case_id")
        condition = item.get("condition")
        replicate = item.get("replicate")
        _require(isinstance(case_id, str) and case_id in cases, "v3 plan slot references an unknown case")
        _require(isinstance(condition, str) and condition in CONDITION_NAMES, "v3 plan slot condition is unsupported")
        _require(_is_count(replicate) and replicate > 0, "v3 plan slot replicate is invalid")
        expected_case, expected_condition, expected_replicate = V3_SLOT_ORDER[order - 1]
        _require(
            (case_id, condition, replicate) == (expected_case, expected_condition, expected_replicate),
            "v3 plan slot identity or order differs from the frozen batch",
        )
        _require(item.get("run_order") == order, "v3 plan slot order is invalid")
        _require(item.get("attempt") == 1, "v3 plan slot attempt is unsupported")
        _require(item.get("attempt_kind") == "independent_replicate", "v3 plan slot attempt kind is unsupported")
        identity = (case_id, condition, replicate)
        _require(identity not in identities, "v3 plan slot identity is duplicated")
        identities.add(identity)
        expected_slots.append({
            "slot_id": f"{case_id}:{condition}:replicate-{replicate}",
            "case_id": case_id,
            "condition": condition,
            "replicate": replicate,
            "run_order": order,
        })
    _require(len(identities) == 12, "v3 plan does not contain 12 unique case/condition/replicate slots")
    _require(_is_sha256(hashes.get("plan_sha256")), "v3 plan hash is invalid")
    immutable_plan = {key: value for key, value in plan.items() if key not in {"plan_state", "execution_authorized"}}
    immutable_sha256 = _sha256((json.dumps(
        immutable_plan, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8"))
    _require(
        immutable_sha256 == report.get("immutable_plan_sha256")
        == dependency_snapshot.get("immutable_plan_sha256"),
        "v3 immutable plan digest does not match the plan and pre-execution snapshot",
    )
    return expected_slots


def _artifact_reference(value: Any, label: str) -> bool:
    if value is None:
        return False
    _require(isinstance(value, dict), f"v3 {label} artifact reference is invalid")
    _require(
        isinstance(value.get("path"), str) and value["path"] and "\x00" not in value["path"],
        f"v3 {label} artifact path is missing or invalid",
    )
    _require(_is_sha256(value.get("sha256")), f"v3 {label} artifact hash is invalid")
    _require(_is_count(value.get("bytes")), f"v3 {label} artifact byte count is invalid")
    return True


def _validate_v3_eval_summary(
    report: dict[str, Any],
    ab_summary: dict[str, Any] | None,
    run_record_count: int,
    run_record_case_ids: set[str],
    benchmark_id: str = V3_BENCHMARK_ID,
) -> dict[str, Any]:
    human = report.get("human_quality")
    _require(isinstance(human, dict), "v3 human quality state is missing")
    _require(human.get("annotation_status") == "pending", "v3 human labels must remain pending")
    _require(human.get("human_labels_read") is False, "v3 seal must not have read human labels")
    _require(all(human.get(field) is None for field in (
        "precision", "recall", "clean_case_false_alarms", "wrong_resolution_count"
    )), "v3 seal human quality metrics must remain null")

    by_condition = ab_summary.get("by_condition") if ab_summary is not None else None
    if run_record_count and ab_summary is not None:
        _require(isinstance(by_condition, dict), "v3 ab_eval condition summary is missing")
        for condition in CONDITION_NAMES.values():
            values = by_condition.get(condition)
            _require(isinstance(values, dict), "v3 ab_eval condition summary is invalid")
            _require(
                values.get("human_precision_mean") is None
                and values.get("known_defect_recall_mean") is None
                and values.get("human_precision_case_count") == 0
                and values.get("known_defect_recall_case_count") == 0,
                "v3 pending labels must not produce human quality metrics",
            )

    ab_eval = report.get("ab_eval")
    _require(isinstance(ab_eval, dict), "v3 seal strict ab_eval state is missing")
    status = ab_eval.get("status")
    _require(status in {
        "not_generated", "validated", "invalid_run_record",
        "blocked_label_reference_present", "invalid",
    }, "v3 seal strict ab_eval status is unsupported")
    if ab_summary is None:
        _require(status == "not_generated", "v3 ab_eval summary is required when strict eval was generated")
        _require(run_record_count == 0, "v3 ab_eval summary is required when run records exist")
        _require(ab_eval.get("pairs") == [], "v3 empty strict ab_eval must not contain pairs")
        return {
            "status": status,
            "run_record_count": 0,
            "adjudicated_case_count": 0,
            "pending_annotation_case_count": 0,
            "pairs": [],
        }

    _require(ab_summary.get("schema_version") == 1, "v3 ab_eval summary schema is unsupported")
    _require(ab_summary.get("run_count") == run_record_count, "v3 ab_eval run count disagrees with sealed run records")
    _require(_is_count(ab_summary.get("adjudicated_case_count")), "v3 adjudicated case count is invalid")
    _require(_is_count(ab_summary.get("pending_annotation_case_count")), "v3 pending annotation count is invalid")
    _require(ab_summary["adjudicated_case_count"] == 0, "v3 export requires human labels to remain pending")
    _require(
        ab_summary["pending_annotation_case_count"] == len(run_record_case_ids),
        "v3 pending annotation count disagrees with sealed run-record cases",
    )
    if status == "not_generated":
        _require(run_record_count == 0, "v3 strict ab_eval cannot be not generated with run records")
        _require(ab_eval.get("pairs") == [], "v3 empty strict ab_eval must not contain pairs")
        _require(
            ab_summary.get("benchmark_id") is None,
            "v3 empty ab_eval summary must not claim a benchmark run",
        )
        _require(ab_summary.get("paired_cases", []) == [], "v3 empty ab_eval summary must not contain pairs")
        raw_pairs: list[Any] = []
    elif status == "validated":
        _require(run_record_count > 0, "v3 seal cannot validate strict ab_eval without run records")
        _require(ab_summary.get("benchmark_id") == benchmark_id, "v3 ab_eval summary benchmark id is invalid")
        raw_pairs = ab_summary.get("paired_cases")
        _require(isinstance(raw_pairs, list), "v3 ab_eval paired cases are missing")
        projected_pairs = []
        seen_pairs: set[tuple[str, int | None]] = set()
        for pair in raw_pairs:
            _require(isinstance(pair, dict), "v3 ab_eval pair must be an object")
            case_id = pair.get("case_id")
            replicate = pair.get("replicate")
            pair_status = pair.get("status")
            _require(
                isinstance(case_id, str) and case_id in {"case0002", "case0003", "case0006"},
                "v3 ab_eval pair case is unknown",
            )
            _require(replicate is None or (_is_count(replicate) and replicate > 0), "v3 ab_eval pair replicate is invalid")
            _require(isinstance(pair_status, str) and pair_status, "v3 ab_eval pair status is missing")
            identity = (case_id, replicate)
            _require(identity not in seen_pairs, "v3 ab_eval pair is duplicated")
            seen_pairs.add(identity)
            projected_pairs.append({"case_id": case_id, "replicate": replicate, "status": pair_status})
        _require(ab_eval.get("pairs") == projected_pairs, "v3 seal pair projection differs from strict ab_eval summary")

        for pair in raw_pairs:
            precision = pair.get("human_precision")
            recall = pair.get("known_defect_recall")
            for metric in (precision, recall):
                if metric is not None:
                    _require(
                        isinstance(metric, dict) and all(value is None for value in metric.values()),
                        "v3 pending labels must not produce pair-level human quality metrics",
                    )
    else:
        _require(run_record_count > 0, "v3 blocked or invalid strict ab_eval requires sealed run records")
        _require(ab_eval.get("pairs") == [], "v3 blocked or invalid strict ab_eval must not contain pairs")
        _require(ab_summary.get("paired_cases", []) == [], "v3 blocked or invalid summary must not contain pairs")
        raw_pairs = []

    return {
        "status": status,
        "run_record_count": run_record_count,
        "adjudicated_case_count": 0,
        "pending_annotation_case_count": ab_summary["pending_annotation_case_count"],
        "pairs": raw_pairs,
    }


def _source_hashes(
    seal_report_bytes: bytes,
    ab_summary_bytes: bytes,
    dependency_snapshot_bytes: bytes,
    plan_bytes: bytes,
    runtime_config_bytes: bytes,
    expected_source_hashes: dict[str, str] | None,
    export_id: str = EXPORT_ID,
) -> dict[str, str]:
    hashes = {
        "seal_report_sha256": _sha256(seal_report_bytes),
        "ab_summary_sha256": _sha256(ab_summary_bytes),
        "dependency_snapshot_file_sha256": _sha256(dependency_snapshot_bytes),
        "plan_sha256": _sha256(plan_bytes),
        "runtime_config_sha256": _sha256(runtime_config_bytes),
    }
    expected = _source_hash_policy(export_id, expected_source_hashes)
    for name, digest in expected.items():
        hash_key = {"seal_report": "seal_report_sha256", "ab_summary": "ab_summary_sha256"}.get(name)
        if hash_key is not None:
            _require(hashes[hash_key] == digest, f"pinned {name} byte hash does not match")
    return hashes


def _validate_source_objects(
    report: Any,
    ab_summary: Any,
    dependency_snapshot: Any,
    hashes: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    _require(isinstance(report, dict), "seal report must be an object")
    _require(report.get("type") == "workflow_only_batch_seal", "seal report type is unsupported")
    _require(report.get("seal_status") == "accepted", "seal integrity status is not accepted")
    _require(isinstance(ab_summary, dict), "ab_eval summary must be an object")
    _require(isinstance(dependency_snapshot, dict), "dependency snapshot must be an object")

    dependencies = report.get("dependencies")
    _require(isinstance(dependencies, dict), "seal report dependency state is missing")
    _require(dependencies.get("status") == "unchanged", "seal report dependencies changed")
    _require(dependencies.get("plan_changed") is False, "seal report records a changed plan")
    _require(dependencies.get("runtime_config_changed") is False, "seal report records a changed runtime config")
    _require(dependencies.get("changed_paths") == [], "seal report changed paths are not empty")
    _require(
        dependencies.get("pre_execution_snapshot_sha256") == hashes["dependency_snapshot_file_sha256"],
        "dependency snapshot bytes do not match the seal report binding",
    )
    _require(report.get("plan_sha256") == hashes["plan_sha256"], "plan bytes do not match the seal report binding")
    _require(
        report.get("runtime_config_sha256") == hashes["runtime_config_sha256"],
        "runtime config bytes do not match the seal report binding",
    )
    _require(
        dependency_snapshot.get("schema_version") == 1
        and dependency_snapshot.get("type") == "pre_execution_snapshot",
        "dependency snapshot type or schema is unsupported",
    )

    artifact_report = report.get("sealed_artifacts")
    _require(isinstance(artifact_report, dict), "sealed artifact check is missing")
    _require(artifact_report.get("status") == "no_mismatches", "sealed artifact references contain mismatches")
    _require(artifact_report.get("mismatches") == [], "sealed artifact mismatch list is not empty")
    references_checked = artifact_report.get("references_checked")
    unavailable_results = artifact_report.get("unavailable_slot_results")
    _require(_is_count(references_checked), "sealed artifact reference count is invalid")
    _require(_is_count(unavailable_results), "unavailable slot result count is invalid")

    raw_slots = report.get("slots")
    _require(isinstance(raw_slots, list) and len(raw_slots) == 12, "seal report must retain all 12 planned slots")
    slots: list[dict[str, Any]] = []
    slot_ids: set[str] = set()
    unavailable_count = 0
    for slot in raw_slots:
        _require(isinstance(slot, dict), "seal report slot must be an object")
        slot_id = slot.get("slot_id")
        condition = slot.get("condition")
        _require(isinstance(slot_id, str) and slot_id not in slot_ids, "seal report slot identity is missing or duplicated")
        _require(isinstance(slot.get("case_id"), str) and slot.get("case_id"), "seal report slot case id is missing")
        _require(_is_count(slot.get("replicate")) and slot["replicate"] > 0, "seal report slot replicate is invalid")
        _require(_is_count(slot.get("run_order")) and slot["run_order"] > 0, "seal report slot run order is invalid")
        _require(condition in CONDITION_NAMES, "seal report slot condition is unsupported")
        _require(slot.get("planned") is True, "seal report contains an unplanned slot")
        _require(isinstance(slot.get("started"), bool), "seal report slot launch state is invalid")
        _require(isinstance(slot.get("host_completed"), bool), "seal report host completion state is invalid")
        _require(isinstance(slot.get("evidence_valid"), bool), "seal report evidence state is invalid")
        _require(isinstance(slot.get("status"), str), "seal report slot status is missing")
        slot_check = slot.get("sealed_artifact_check")
        _require(isinstance(slot_check, dict), "seal report per-slot artifact check is missing")
        if slot_check.get("status") == "not_applicable":
            unavailable_count += 1
        status = slot_check.get("status")
        _require(status in {"verified", "mismatch", "not_applicable"}, "seal report per-slot artifact status is invalid")
        _require(slot_check.get("status") != "mismatch", "seal report per-slot artifact references contain mismatches")
        _require(_is_count(slot_check.get("references_checked")), "seal report per-slot reference count is invalid")
        _require(isinstance(slot_check.get("mismatches"), list), "seal report per-slot mismatch list is invalid")
        _require(slot_check["mismatches"] == [], "seal report per-slot mismatch list is not empty")
        slot_ids.add(slot_id)

        launched = slot["started"]
        host_completed = slot["host_completed"]
        if not launched:
            outcome = "not_started"
        elif host_completed:
            outcome = "host_completed"
        elif "failure" in slot["status"] or slot["status"] == "failed":
            outcome = "failure"
        else:
            outcome = "incomplete"
        slots.append({
            "slot_id": slot_id,
            "case_id": slot.get("case_id"),
            "condition": condition,
            "condition_name": CONDITION_NAMES[condition],
            "replicate": slot.get("replicate"),
            "run_order": slot.get("run_order"),
            "planned": True,
            "launched": launched,
            "host_completed": host_completed,
            "evidence_valid": slot["evidence_valid"],
            "status": slot["status"],
            "outcome": outcome,
        })

    _require(unavailable_count == unavailable_results, "aggregate unavailable slot result count disagrees with per-slot checks")
    _require(artifact_report.get("references_checked") == sum(
        slot["sealed_artifact_check"]["references_checked"] for slot in raw_slots
    ), "aggregate artifact reference count disagrees with per-slot checks")

    pairs = report.get("pairs")
    _require(isinstance(pairs, list) and len(pairs) == 6, "seal report must retain all 6 planned pairs")
    projected_pairs = []
    pair_ids: set[tuple[str, int]] = set()
    for pair in pairs:
        _require(isinstance(pair, dict), "seal report pair must be an object")
        case_id = pair.get("case_id")
        replicate = pair.get("replicate")
        status = pair.get("ab_eval_status")
        _require(isinstance(case_id, str) and _is_count(replicate) and replicate > 0, "seal report pair identity is invalid")
        _require((case_id, replicate) not in pair_ids, "seal report pair identity is duplicated")
        _require(isinstance(status, str) and status, "seal report pair status is missing")
        _require(isinstance(pair.get("eligible"), bool), "seal report pair eligibility is invalid")
        _require(isinstance(pair.get("slots"), list) and len(pair["slots"]) == 2, "seal report pair slot list is invalid")
        _require(all(isinstance(slot_id, str) and slot_id in slot_ids for slot_id in pair["slots"]), "seal report pair references an unknown slot")
        pair_ids.add((case_id, replicate))
        projected_pairs.append({
            "case_id": case_id,
            "replicate": replicate,
            "status": status,
            "eligible": pair["eligible"],
            "slots": list(pair["slots"]),
        })

    report_counts = report.get("counts")
    _require(isinstance(report_counts, dict), "seal report counts are missing")
    _require(report_counts.get("planned_slots") == 12, "seal report planned slot count is invalid")
    recomputed_counts = {
        "planned_slots": len(slots),
        "launched_slots": sum(slot["launched"] for slot in slots),
        "host_completed_slots": sum(slot["host_completed"] for slot in slots),
        "evidence_valid_slots": sum(slot["evidence_valid"] for slot in slots),
        "not_started_slots": sum(slot["outcome"] == "not_started" for slot in slots),
        "failed_slots": sum(slot["outcome"] == "failure" for slot in slots),
        "eligible_pairs": sum(pair["eligible"] for pair in projected_pairs),
        "planned_pairs": len(projected_pairs),
    }
    _require(report_counts.get("started_slots") == recomputed_counts["launched_slots"], "seal report launched slot count disagrees with slot records")
    _require(report_counts.get("host_completed_slots") == recomputed_counts["host_completed_slots"], "seal report host completion count disagrees with slot records")
    _require(report_counts.get("evidence_valid_slots") == recomputed_counts["evidence_valid_slots"], "seal report evidence count disagrees with slot records")
    _require(report_counts.get("eligible_pairs") == recomputed_counts["eligible_pairs"], "seal report eligible pair count disagrees with pair records")
    _require(report_counts.get("planned_pairs") == recomputed_counts["planned_pairs"], "seal report planned pair count disagrees with pair records")

    _require(ab_summary.get("run_count") == 0, "final ab_eval summary must report zero run records")
    _require(ab_summary.get("adjudicated_case_count") == 0, "final ab_eval summary must contain no adjudicated cases")
    ab_eval_state = report.get("ab_eval")
    _require(isinstance(ab_eval_state, dict) and ab_eval_state.get("status") == "not_generated", "strict ab_eval summary must remain not generated")
    human_quality = report.get("human_quality")
    _require(isinstance(human_quality, dict), "human quality state is missing")
    _require(human_quality.get("human_labels_read") is False, "human labels must remain unread")
    _require(all(human_quality.get(field) is None for field in (
        "precision", "recall", "clean_case_false_alarms", "wrong_resolution_count"
    )), "human quality metrics must remain null")
    _require(report.get("benchmark_id") == dependency_snapshot.get("benchmark_id"), "seal report and dependency snapshot benchmark ids differ")
    _require(_is_sha256(report.get("plan_sha256")), "seal report plan hash is invalid")
    _require(_is_sha256(report.get("runtime_config_sha256")), "seal report runtime config hash is invalid")
    _require(_is_sha256(dependencies.get("pre_execution_snapshot_sha256")), "seal report dependency snapshot hash is invalid")

    seal_completeness = "complete" if unavailable_results == 0 and references_checked > 0 else "incomplete"
    return slots, projected_pairs, {
        "report_counts": report_counts,
        "recomputed_counts": recomputed_counts,
        "references_checked": references_checked,
        "unavailable_slot_results": unavailable_results,
        "seal_completeness": seal_completeness,
    }


def _validate_v3_source_objects(
    report: Any,
    ab_summary: Any | None,
    dependency_snapshot: Any,
    plan: Any,
    hashes: dict[str, str],
    benchmark_id: str = V3_BENCHMARK_ID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    _require(isinstance(report, dict), "v3 seal report must be an object")
    _require(ab_summary is None or isinstance(ab_summary, dict), "v3 ab_eval summary must be an object")
    _require(isinstance(dependency_snapshot, dict), "v3 dependency snapshot must be an object")
    _require(report.get("schema_version") == 1, "v3 seal report schema is unsupported")
    _require(report.get("type") == "workflow_only_batch_seal", "v3 seal report type is unsupported")
    _require(report.get("benchmark_id") == benchmark_id, "v3 seal report id is unsupported")
    _require(report.get("seal_status") == "accepted", "v3 seal integrity status is not accepted")
    _require(dependency_snapshot.get("schema_version") == 1, "v3 dependency snapshot schema is unsupported")
    _require(dependency_snapshot.get("type") == "pre_execution_snapshot", "v3 dependency snapshot type is unsupported")
    _require(dependency_snapshot.get("benchmark_id") == benchmark_id, "v3 dependency snapshot id is unsupported")
    source_commit = report.get("source_commit")
    _require(
        isinstance(source_commit, str)
        and len(source_commit) == 40
        and all(char in HEX_DIGITS for char in source_commit)
        and source_commit == dependency_snapshot.get("source_commit"),
        "v3 seal source commit differs from snapshot or is invalid",
    )
    _require(report.get("plan_sha256") == hashes["plan_sha256"], "v3 plan bytes do not match the seal report binding")
    _require(report.get("runtime_config_sha256") == hashes["runtime_config_sha256"], "v3 runtime config bytes do not match the seal report binding")
    dependencies = report.get("dependencies")
    _require(isinstance(dependencies, dict), "v3 seal dependency state is missing")
    _require(
        dependency_snapshot.get("plan_sha256") == dependencies.get("pre_execution_plan_sha256"),
        "v3 pre-execution plan binding differs from the seal report",
    )
    _require(dependencies.get("status") == "unchanged", "v3 seal dependencies changed")
    _require(dependencies.get("changed_paths") == [], "v3 seal changed paths are not empty")
    _require(dependencies.get("plan_changed") is False, "v3 seal records a changed immutable plan")
    _require(dependencies.get("source_commit_changed") is False, "v3 seal records a changed source commit")
    _require(dependencies.get("runtime_config_changed") is False, "v3 seal records a changed runtime config")
    _require(
        dependencies.get("pre_execution_snapshot_sha256") == hashes["dependency_snapshot_file_sha256"],
        "v3 dependency snapshot bytes do not match the seal report binding",
    )
    _require(_is_sha256(dependencies.get("pre_execution_plan_sha256")), "v3 pre-execution plan digest is invalid")
    _require(_is_sha256(dependencies.get("pre_execution_snapshot_sha256")), "v3 pre-execution snapshot digest is invalid")
    _require(_is_sha256(report.get("immutable_plan_sha256")), "v3 immutable plan digest is invalid")
    _require(_is_sha256(report.get("plan_sha256")), "v3 plan digest is invalid")
    _require(_is_sha256(report.get("runtime_config_sha256")), "v3 runtime config digest is invalid")
    _require(
        dependency_snapshot.get("immutable_plan_sha256") == report.get("immutable_plan_sha256"),
        "v3 immutable plan digest differs from pre-execution snapshot",
    )
    expected_slots = _validate_v3_plan(report, plan, dependency_snapshot, hashes, benchmark_id)
    snapshot_slots = dependency_snapshot.get("slots")
    _require(isinstance(snapshot_slots, list) and len(snapshot_slots) == len(expected_slots),
        "v3 dependency snapshot must retain every prepared slot")
    snapshot_slot_by_id: dict[str, dict[str, Any]] = {}
    for expected, item in zip(expected_slots, snapshot_slots, strict=True):
        _require(isinstance(item, dict), "v3 dependency snapshot slot is invalid")
        _require(item.get("slot_id") == expected["slot_id"], "v3 dependency snapshot slot order differs from plan")
        _require(item.get("immutable_plan_sha256") == report.get("immutable_plan_sha256"),
            "v3 dependency snapshot slot immutable plan digest is invalid")
        _require(_is_sha256(item.get("source_bundle_manifest_sha256")),
            "v3 dependency snapshot source bundle digest is invalid")
        _require(_is_count(item.get("manifest_file_count")) and item["manifest_file_count"] > 0,
            "v3 dependency snapshot source bundle file count is invalid")
        snapshot_slot_by_id[item["slot_id"]] = item

    artifact_report = report.get("sealed_artifacts")
    _require(isinstance(artifact_report, dict), "v3 sealed artifact check is missing")
    _require(artifact_report.get("status") == "no_mismatches", "v3 sealed artifact references contain mismatches")
    _require(artifact_report.get("mismatches") == [], "v3 sealed artifact mismatch list is not empty")
    aggregate_references = artifact_report.get("references_checked")
    unavailable_results = artifact_report.get("unavailable_slot_results")
    _require(_is_count(aggregate_references), "v3 sealed artifact reference count is invalid")
    _require(_is_count(unavailable_results), "v3 unavailable slot result count is invalid")

    raw_slots = report.get("slots")
    _require(isinstance(raw_slots, list) and len(raw_slots) == len(expected_slots), "v3 seal report must retain every planned slot")
    artifact_fields = {
        "slot_result", "run_record", "execution", "adapter_start", "adapter_failure",
        "codex_events", "raw_output", "host_output", "raw_trace", "supervisor_events",
        "source_bundle_manifest", "runtime_evidence",
    }
    slots: list[dict[str, Any]] = []
    slot_by_id: dict[str, dict[str, Any]] = {}
    reference_count = 0
    unavailable_count = 0
    run_record_case_ids: set[str] = set()
    run_record_count = 0
    for expected, raw in zip(expected_slots, raw_slots, strict=True):
        _require(isinstance(raw, dict), "v3 seal report slot must be an object")
        for field in ("slot_id", "case_id", "condition", "replicate", "run_order"):
            _require(raw.get(field) == expected[field], f"v3 seal slot {field} differs from frozen plan")
        _require(raw.get("planned") is True, "v3 seal report contains an unplanned slot")
        _require(isinstance(raw.get("started"), bool), "v3 seal slot launch state is invalid")
        _require(isinstance(raw.get("host_completed"), bool), "v3 seal slot host completion state is invalid")
        _require(isinstance(raw.get("evidence_valid"), bool), "v3 seal slot evidence state is invalid")
        _require(not raw["host_completed"] or raw["started"], "v3 completed slot was not started")
        _require(not raw["evidence_valid"] or raw["started"], "v3 valid evidence belongs to a slot that was not started")
        _require(raw.get("status") in {"completed", "failed", "not_started"}, "v3 seal slot status is unsupported")
        _require(
            (raw["status"] == "not_started" and not raw["started"] and not raw["host_completed"])
            or (raw["status"] == "failed" and raw["started"] and not raw["host_completed"])
            or (raw["status"] == "completed" and raw["started"] and raw["host_completed"]),
            "v3 seal slot status disagrees with launch or host completion state",
        )
        _require(raw.get("preseal_manifest_matches_snapshot") is True,
            "v3 seal source bundle differs from pre-execution snapshot")
        snapshot_slot = snapshot_slot_by_id[expected["slot_id"]]
        _require(raw.get("source_bundle_manifest_sha256") == snapshot_slot["source_bundle_manifest_sha256"],
            "v3 seal source bundle digest differs from pre-execution snapshot")
        source_bundle_check = raw.get("source_bundle_check")
        _require(isinstance(source_bundle_check, dict), "v3 source bundle check is missing")
        _require(source_bundle_check.get("status") == "verified" and source_bundle_check.get("mismatches") == [],
            "v3 source bundle has mismatches")
        _require(source_bundle_check.get("files_checked") == snapshot_slot["manifest_file_count"],
            "v3 source bundle file count differs from pre-execution snapshot")
        check = raw.get("sealed_artifact_check")
        _require(isinstance(check, dict), "v3 per-slot artifact check is missing")
        check_status = check.get("status")
        _require(check_status in {"verified", "not_applicable"}, "v3 per-slot artifact check is invalid")
        checked = check.get("references_checked")
        _require(_is_count(checked), "v3 per-slot artifact reference count is invalid")
        _require(check.get("mismatches") == [], "v3 per-slot artifact mismatch list is not empty")
        artifacts = raw.get("artifacts")
        _require(isinstance(artifacts, dict) and set(artifacts) == artifact_fields, "v3 slot artifact inventory is invalid")
        if check_status == "verified":
            _require(checked > 0, "v3 verified artifact check has no references")
            _require(artifacts.get("slot_result") is not None, "v3 verified artifact check has no slot result")
        else:
            _require(checked == 0, "v3 unavailable slot result has checked artifact references")
            _require(artifacts.get("slot_result") is None, "v3 unavailable slot result has an artifact reference")
            unavailable_count += 1
        reference_count += checked

        for name, reference in artifacts.items():
            if _artifact_reference(reference, name):
                path = reference["path"]
                _require("\\" not in path and not Path(path).is_absolute() and ".." not in Path(path).parts
                    and not (len(path) > 1 and path[1] == ":"),
                    f"v3 {name} artifact path is unsafe")
            if name == "run_record" and reference is not None:
                run_record_count += 1
                run_record_case_ids.add(raw["case_id"])
        manifest_ref = artifacts["source_bundle_manifest"]
        _require(manifest_ref is not None, "v3 prepared source bundle manifest reference is missing")
        _require(manifest_ref["sha256"] == snapshot_slot["source_bundle_manifest_sha256"],
            "v3 source bundle artifact reference differs from pre-execution snapshot")

        if not raw["started"]:
            outcome = "not_started"
        elif raw["host_completed"]:
            outcome = "host_completed"
        elif raw["status"].lower() == "failed" or "failure" in raw["status"].lower():
            outcome = "failure"
        else:
            outcome = "incomplete"
        slot = {
            "slot_id": expected["slot_id"],
            "case_id": expected["case_id"],
            "condition": expected["condition"],
            "condition_name": CONDITION_NAMES[expected["condition"]],
            "replicate": expected["replicate"],
            "run_order": expected["run_order"],
            "planned": True,
            "launched": raw["started"],
            "host_completed": raw["host_completed"],
            "evidence_valid": raw["evidence_valid"],
            "status": raw["status"],
            "outcome": outcome,
        }
        slots.append(slot)
        slot_by_id[slot["slot_id"]] = slot

    _require(reference_count == aggregate_references, "v3 aggregate artifact count disagrees with per-slot checks")
    _require(unavailable_count == unavailable_results, "v3 unavailable slot result count disagrees with per-slot checks")
    strict_eval = _validate_v3_eval_summary(report, ab_summary, run_record_count, run_record_case_ids, benchmark_id)

    strict_pair_status: dict[tuple[str, int], str] = {}
    for pair in strict_eval["pairs"]:
        _require(
            isinstance(pair.get("case_id"), str)
            and pair.get("case_id") in {"case0002", "case0003", "case0006"},
            "v3 strict pair case is unknown",
        )
        _require(pair.get("replicate") is None or pair.get("replicate") in {1, 2}, "v3 strict pair replicate is invalid")
        if pair.get("replicate") is not None:
            strict_pair_status[(pair["case_id"], pair["replicate"])] = pair["status"]
    report_pairs = report.get("pairs")
    _require(isinstance(report_pairs, list) and len(report_pairs) == 6, "v3 seal report must retain all six planned pairs")
    expected_pair_ids = [(case_id, replicate) for case_id in ("case0002", "case0003", "case0006") for replicate in (1, 2)]
    pairs: list[dict[str, Any]] = []
    for expected_identity, raw_pair in zip(expected_pair_ids, report_pairs, strict=True):
        _require(isinstance(raw_pair, dict), "v3 seal report pair must be an object")
        case_id, replicate = expected_identity
        _require((raw_pair.get("case_id"), raw_pair.get("replicate")) == expected_identity,
            "v3 seal pair identity or order differs from frozen plan")
        expected_status = strict_pair_status.get(expected_identity, "pair_incomplete_missing_run_record")
        _require(raw_pair.get("ab_eval_status") == expected_status, "v3 seal pair status disagrees with strict summary")
        slot_ids = [f"{case_id}:A:replicate-{replicate}", f"{case_id}:B:replicate-{replicate}"]
        _require(raw_pair.get("slots") == slot_ids, "v3 seal pair slot references differ from frozen plan")
        eligible = (
            strict_eval["status"] == "validated"
            and expected_status == V3_ELIGIBLE_PAIR_STATUS
            and all(slot_by_id[slot_id]["host_completed"] and slot_by_id[slot_id]["evidence_valid"] for slot_id in slot_ids)
            and dependencies["status"] == "unchanged"
        )
        _require(raw_pair.get("eligible") is eligible, "v3 seal pair eligibility disagrees with slot evidence")
        pairs.append({
            "case_id": case_id,
            "replicate": replicate,
            "status": expected_status,
            "eligible": eligible,
            "slots": slot_ids,
        })

    recomputed_counts = {
        "planned_slots": len(slots),
        "launched_slots": sum(slot["launched"] for slot in slots),
        "host_completed_slots": sum(slot["host_completed"] for slot in slots),
        "evidence_valid_slots": sum(slot["evidence_valid"] for slot in slots),
        "not_started_slots": sum(slot["outcome"] == "not_started" for slot in slots),
        "failed_slots": sum(slot["outcome"] == "failure" for slot in slots),
        "eligible_pairs": sum(pair["eligible"] for pair in pairs),
        "planned_pairs": len(pairs),
    }
    report_counts = report.get("counts")
    _require(isinstance(report_counts, dict), "v3 seal report counts are missing")
    expected_report_counts = {
        "planned_slots": "planned_slots", "started_slots": "launched_slots",
        "host_completed_slots": "host_completed_slots", "evidence_valid_slots": "evidence_valid_slots",
        "eligible_pairs": "eligible_pairs", "planned_pairs": "planned_pairs",
    }
    for report_key, projected_key in expected_report_counts.items():
        value = report_counts.get(report_key)
        _require(_is_count(value) and value == recomputed_counts[projected_key],
            f"v3 seal {report_key} count disagrees with slot and pair records")
    artifact_completeness = "complete" if unavailable_results == 0 and aggregate_references > 0 else "incomplete"
    prepared_scope = report.get("prepared_input_scope")
    _require(isinstance(prepared_scope, dict), "v3 prepared input scope is missing")
    _require(prepared_scope.get("purpose") == "workflow_reproducibility_validation_of_existing_corpus",
        "v3 prepared input purpose is unsupported")
    _require(prepared_scope.get("fresh_blind_test") is False, "v3 seal must not claim a fresh blind test")
    model_cost = report.get("model_cost")
    _require(isinstance(model_cost, dict), "v3 model cost state is missing")
    _require(model_cost.get("status") == "unknown", "v3 model cost must remain unknown")
    _require(model_cost.get("amount") is None and model_cost.get("currency") is None,
        "v3 model cost values must remain null")
    current_hashes = dependencies.get("current_hashes")
    _require(isinstance(current_hashes, dict), "v3 seal dependency hash map is missing")
    seal_tool_hash = current_hashes.get("external/workflow_batch_seal.py")
    _require(_is_sha256(seal_tool_hash), "v3 seal helper hash is invalid")
    consumers = dependency_snapshot.get("consumers")
    _require(isinstance(consumers, dict), "v3 dependency snapshot tool versions are missing")
    _require(isinstance(consumers.get("ab_eval"), str) and consumers["ab_eval"], "v3 ab_eval version is missing")
    _require(isinstance(consumers.get("runtime_normalizer"), (int, str))
        and not isinstance(consumers["runtime_normalizer"], bool), "v3 runtime normalizer version is missing")

    return slots, pairs, {
        "recomputed_counts": recomputed_counts,
        "references_checked": aggregate_references,
        "unavailable_slot_results": unavailable_results,
        "seal_completeness": artifact_completeness,
        "strict_eval": strict_eval,
        "run_record_case_ids": sorted(run_record_case_ids),
        "run_record_count": run_record_count,
        "seal_tool_hash": seal_tool_hash,
        "consumers": consumers,
        "plan_sha256": hashes["plan_sha256"],
    }


def _validate_closure_sources(
    closure: Any,
    post_snapshot: Any,
    partial_report: Any,
    pre_snapshot: Any,
    plan: Any,
    ab_summary: Any | None,
    hashes: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    _require(isinstance(closure, dict), "workflow closure report must be an object")
    _require(isinstance(post_snapshot, dict), "post-execution snapshot must be an object")
    _require(closure.get("schema_version") == 1, "workflow closure schema is unsupported")
    _require(closure.get("type") == "independent_final_failure_report", "workflow closure type is unsupported")
    _require(closure.get("execution_state") == "closed_incomplete", "workflow closure is not closed incomplete")
    _require(closure.get("plan_state") == "go" and closure.get("execution_authorized") is True,
        "workflow closure plan authorization is invalid")
    benchmark_id = closure.get("benchmark_id")
    _require(isinstance(benchmark_id, str) and benchmark_id, "workflow closure benchmark id is missing")
    _require(closure.get("plan_sha256") == hashes["plan_sha256"], "plan bytes do not match the closure binding")
    _require(closure.get("immutable_plan_sha256") == pre_snapshot.get("immutable_plan_sha256"),
        "closure immutable plan digest differs from pre-execution snapshot")
    _require(_is_sha256(closure.get("immutable_plan_sha256")), "closure immutable plan digest is invalid")

    artifact_hashes = closure.get("artifact_hashes")
    _require(isinstance(artifact_hashes, dict), "closure artifact hash map is missing")
    _require(artifact_hashes.get("plan") == hashes["plan_sha256"], "plan bytes do not match closure artifact hash")
    _require(artifact_hashes.get("runtime_config") == hashes["runtime_config_sha256"],
        "runtime config bytes do not match closure artifact hash")

    sealing = closure.get("sealing")
    _require(isinstance(sealing, dict), "closure sealing bindings are missing")
    for field, hash_key, message in (
        ("pre_execution_snapshot_sha256", "pre_execution_snapshot_sha256", "pre-execution snapshot"),
        ("post_execution_snapshot_sha256", "post_execution_snapshot_sha256", "post-execution snapshot"),
        ("partial_workflow_report_sha256", "partial_workflow_report_sha256", "partial workflow report"),
    ):
        _require(sealing.get(field) == hashes[hash_key], f"{message} bytes do not match the closure binding")
    _require(sealing.get("partial_workflow_seal_status") == "accepted",
        "closure partial workflow report is not accepted")
    _require(sealing.get("changed_dependencies") == 0, "closure records changed dependencies")

    _require(isinstance(partial_report, dict), "partial workflow report must be an object")
    _require(isinstance(pre_snapshot, dict), "pre-execution snapshot must be an object")
    _require(isinstance(plan, dict), "batch plan must be an object")
    _require(post_snapshot.get("schema_version") == 1, "post-execution snapshot schema is unsupported")
    _require(post_snapshot.get("type") == "post_execution_dependency_snapshot",
        "post-execution snapshot type is unsupported")
    _require(pre_snapshot.get("benchmark_id") == benchmark_id == partial_report.get("benchmark_id")
        == post_snapshot.get("benchmark_id"), "closure source benchmark ids differ")
    _require(post_snapshot.get("source_commit") == pre_snapshot.get("source_commit")
        == partial_report.get("source_commit"), "closure source commit bindings differ")
    _require(post_snapshot.get("immutable_plan_sha256") == closure.get("immutable_plan_sha256")
        == partial_report.get("immutable_plan_sha256"), "closure immutable plan bindings differ")
    _require(post_snapshot.get("plan_sha256") == closure.get("plan_sha256") == partial_report.get("plan_sha256"),
        "closure plan hash bindings differ")
    _require(post_snapshot.get("runtime_config_sha256") == hashes["runtime_config_sha256"]
        == partial_report.get("runtime_config_sha256"), "closure runtime config bindings differ")

    partial_dependencies = partial_report.get("dependencies")
    _require(isinstance(partial_dependencies, dict), "partial workflow dependency state is missing")
    pre_dependencies = pre_snapshot.get("dependency_hashes")
    post_dependencies = post_snapshot.get("dependency_hashes")
    _require(isinstance(pre_dependencies, dict) and isinstance(post_dependencies, dict),
        "closure dependency hash maps are missing")
    _require(partial_dependencies.get("current_hashes") == pre_dependencies == post_dependencies,
        "pre, post, and partial workflow dependency hashes differ")
    _require(sealing.get("post_execution_dependency_count") == len(post_dependencies),
        "closure dependency count disagrees with bound hash maps")
    _require(post_snapshot.get("dependency_changed_paths") == [], "post-execution snapshot records changed dependencies")
    _require(post_snapshot.get("plan_state") == "go", "post-execution snapshot plan state is unsupported")

    projected_hashes = {
        **hashes,
        "seal_report_sha256": hashes["partial_workflow_report_sha256"],
        "dependency_snapshot_file_sha256": hashes["pre_execution_snapshot_sha256"],
    }
    slots, pairs, audit = _validate_v3_source_objects(
        partial_report, ab_summary, pre_snapshot, plan, projected_hashes, benchmark_id,
    )

    raw_post_slots = post_snapshot.get("slots")
    _require(isinstance(raw_post_slots, list) and len(raw_post_slots) == len(slots),
        "post-execution snapshot must retain every planned slot")
    outcome_by_id = {slot["slot_id"]: slot for slot in slots}
    started = failed = unstarted = 0
    for order, raw in enumerate(raw_post_slots, start=1):
        _require(isinstance(raw, dict), "post-execution slot must be an object")
        slot_id = raw.get("slot_id")
        _require(slot_id in outcome_by_id and raw.get("run_order") == order,
            "post-execution slot identity or order differs from the plan")
        slot = outcome_by_id[slot_id]
        _require(raw.get("state") in {"started_failed", "unstarted", "started_completed"},
            "post-execution slot state is unsupported")
        state = raw["state"]
        expected_state = {
            "failure": "started_failed",
            "not_started": "unstarted",
            "host_completed": "started_completed",
        }.get(slot["outcome"], "started_failed" if slot["launched"] else "unstarted")
        _require(state == expected_state, "post-execution slot state disagrees with partial workflow report")
        _require(raw.get("source_bundle_manifest_sha256") == partial_report["slots"][order - 1]["source_bundle_manifest_sha256"],
            "post-execution source bundle hash differs from partial workflow report")
        slot_result = partial_report["slots"][order - 1]["artifacts"].get("slot_result")
        expected_result_hash = slot_result.get("sha256") if slot_result is not None else None
        _require(raw.get("slot_result_sha256") == expected_result_hash,
            "post-execution slot result hash differs from partial workflow report")
        _require(raw.get("failure_ledger_sha256") is None, "post-execution failure ledger is unsupported")
        if state == "unstarted":
            unstarted += 1
        else:
            started += 1
            failed += state == "started_failed"

    post_counts = post_snapshot.get("counts")
    _require(isinstance(post_counts, dict), "post-execution snapshot counts are missing")
    for key, value in {
        "planned_slots": len(slots), "started_slots": started,
        "failed_slots": failed, "unstarted_slots": unstarted,
    }.items():
        _require(post_counts.get(key) == value, f"post-execution {key} count disagrees with slot states")

    counts = closure.get("counts")
    _require(isinstance(counts, dict), "closure counts are missing")
    recomputed = audit["recomputed_counts"]
    expected_closure_counts = {
        "planned_slots": recomputed["planned_slots"],
        "started_slots": recomputed["launched_slots"],
        "failed_slots": recomputed["failed_slots"],
        "unstarted_slots": recomputed["not_started_slots"],
        "host_completed_slots": recomputed["host_completed_slots"],
        "evidence_valid_slots": recomputed["evidence_valid_slots"],
        "eligible_pairs": recomputed["eligible_pairs"],
        "planned_pairs": recomputed["planned_pairs"],
        "run_records": audit["run_record_count"],
    }
    for key, value in expected_closure_counts.items():
        _require(counts.get(key) == value, f"closure {key} count disagrees with sealed source records")
    _require(counts.get("score_records") == 0, "closure score record count must be zero")

    attempted = closure.get("attempted_slot")
    failed_slots = [slot for slot in partial_report["slots"] if slot["status"] == "failed"]
    _require(isinstance(attempted, dict) and len(failed_slots) == 1,
        "closure attempted slot does not match the sealed failure")
    _require(attempted.get("slot_id") == failed_slots[0]["slot_id"]
        and attempted.get("run_order") == failed_slots[0]["run_order"],
        "closure attempted slot identity differs from the sealed failure")
    _require(attempted.get("run_status") == "failed" and attempted.get("host_completed") is False
        and attempted.get("evidence_valid") is False and attempted.get("retry_count") == 0,
        "closure attempted slot state is invalid")

    strict = closure.get("strict_postprocessing")
    _require(isinstance(strict, dict), "closure strict postprocessing record is missing")
    build_record = strict.get("build")
    _require(isinstance(build_record, dict) and build_record.get("exit_code") == 0,
        "closure strict build did not exit successfully")
    _require(build_record.get("created_run_records") == audit["run_record_count"],
        "closure strict build count disagrees with sealed run records")
    if audit["run_record_count"] == 0:
        _require(build_record.get("created_run_records") == 0,
            "summary cannot be marked not generated when run records exist")
        for key in ("validate", "score", "summarize"):
            _require(strict.get(key) == "not_applicable; build created zero run records",
                f"closure {key} status is not applicable only for zero run records")
        _require(ab_summary is None,
            "closure attests that no ab_eval summary was generated; use --ab-summary-not-generated")
    else:
        _require(ab_summary is not None, "a real ab_eval summary is required when run records exist")

    audit["strict_postprocessing"] = strict
    return slots, pairs, audit


def _project_summary(
    report: dict[str, Any],
    ab_summary: dict[str, Any],
    slots: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    audit: dict[str, Any],
) -> dict[str, Any]:
    counts = audit["recomputed_counts"]
    pair_status_counts = dict(sorted(Counter(pair["status"] for pair in pairs).items()))
    workflow_status = "failed" if counts["failed_slots"] else (
        "completed" if counts["host_completed_slots"] == counts["planned_slots"] else "incomplete"
    )
    scope = report.get("prepared_input_scope", {})
    human = report.get("human_quality", {})
    return {
        "schema_version": 1,
        "export_id": EXPORT_ID,
        "benchmark_id": report["benchmark_id"],
        "status": workflow_status,
        "purpose": "workflow_integrity_projection",
        "prepared_input_scope": {
            "purpose": scope.get("purpose"),
            "fresh_blind_test": scope.get("fresh_blind_test"),
            "description": scope.get("description"),
        },
        "integrity": {
            "seal_status": report["seal_status"],
            "dependency_status": report["dependencies"]["status"],
            "seal_completeness": audit["seal_completeness"],
            "artifact_reference_check": {
                "reported_status": report["sealed_artifacts"]["status"],
                "references_checked": audit["references_checked"],
                "unavailable_slot_results": audit["unavailable_slot_results"],
            },
            "scope": "Accepted seal status covers dependency, plan, and runtime-config identity consistency; it does not mean the workflow completed or every artifact was verified.",
        },
        "workflow": {
            **counts,
            "status": workflow_status,
            "pair_status_counts": pair_status_counts,
            "slots": slots,
            "pairs": pairs,
        },
        "strict_ab_eval": {
            "run_records_created": 0,
            "build_status": "not_generated",
            "validate_status": "not_applicable",
            "score_status": "not_applicable",
        },
        "human_adjudication": {
            "status": human.get("annotation_status", "pending"),
            "labels_read": False,
            "labels": [],
            "precision": None,
            "recall": None,
            "clean_case_false_alarms": None,
            "wrong_resolution_count": None,
        },
        "model_cost": {
            "status": "unknown",
            "amount": None,
            "currency": None,
        },
        "execution_identity": {
            "status": "unknown",
            "provider": None,
            "model": None,
            "host": None,
        },
        "source_ab_summary": {
            "run_count": ab_summary["run_count"],
            "adjudicated_case_count": ab_summary["adjudicated_case_count"],
        },
    }


def _project_v3_summary(
    report: dict[str, Any],
    ab_summary: dict[str, Any] | None,
    slots: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    audit: dict[str, Any],
    *,
    export_id: str = V3_EXPORT_ID,
    exporter_version: str = V3_EXPORTER_VERSION,
) -> dict[str, Any]:
    counts = audit["recomputed_counts"]
    workflow_status = "failed" if counts["failed_slots"] else (
        "completed" if counts["host_completed_slots"] == counts["planned_slots"] else "incomplete"
    )
    strict_status = audit["strict_eval"]["status"]
    strict_projection = {
        "not_generated": ("not_generated", "not_applicable", "not_applicable"),
        "validated": ("validated", "passed", "passed"),
        "blocked_label_reference_present": ("blocked", "blocked", "blocked"),
        "invalid_run_record": ("invalid", "invalid", "invalid"),
        "invalid": ("invalid", "invalid", "invalid"),
    }[strict_status]
    scope = report["prepared_input_scope"]
    human = report["human_quality"]
    return {
        "schema_version": 1,
        "export_id": export_id,
        "exporter_version": exporter_version,
        "benchmark_id": report["benchmark_id"],
        "status": workflow_status,
        "purpose": "workflow_integrity_projection",
        "prepared_input_scope": {
            "purpose": scope.get("purpose"),
            "fresh_blind_test": scope.get("fresh_blind_test"),
            "description": scope.get("description"),
        },
        "integrity": {
            "seal_status": report["seal_status"],
            "dependency_status": report["dependencies"]["status"],
            "seal_completeness": audit["seal_completeness"],
            "artifact_reference_check": {
                "reported_status": report["sealed_artifacts"]["status"],
                "references_checked": audit["references_checked"],
                "unavailable_slot_results": audit["unavailable_slot_results"],
            },
            "scope": "Accepted seal status covers dependency, frozen plan, runtime-config, and referenced artifact integrity; it does not mean the workflow completed or quality was evaluated.",
        },
        "workflow": {
            **counts,
            "status": workflow_status,
            "pair_status_counts": dict(sorted(Counter(pair["status"] for pair in pairs).items())),
            "slots": slots,
            "pairs": pairs,
        },
        "strict_ab_eval": {
            "run_records_created": audit["run_record_count"],
            "status": strict_status,
            "build_status": strict_projection[0],
            "validate_status": strict_projection[1],
            "score_status": strict_projection[2],
        },
        "human_adjudication": {
            "status": human["annotation_status"],
            "labels_read": False,
            "labels": [],
            "pending_case_count": audit["strict_eval"]["pending_annotation_case_count"],
            "precision": None,
            "recall": None,
            "clean_case_false_alarms": None,
            "wrong_resolution_count": None,
        },
        "model_cost": {
            "status": "unknown",
            "amount": None,
            "currency": None,
        },
        "execution_identity": {
            "status": "unknown",
            "provider": None,
            "model": None,
            "host": None,
        },
        "source_ab_summary": (
            {
                "run_count": ab_summary["run_count"],
                "adjudicated_case_count": ab_summary["adjudicated_case_count"],
                "pending_annotation_case_count": ab_summary["pending_annotation_case_count"],
            }
            if ab_summary is not None else {
                "status": "not_generated",
                "run_count": 0,
                "adjudicated_case_count": 0,
                "pending_annotation_case_count": 0,
            }
        ),
    }


def _build_evidence(
    summary: dict[str, Any],
    summary_bytes: bytes,
    report: dict[str, Any],
    dependency_snapshot: dict[str, Any],
    hashes: dict[str, str],
    export_id: str = EXPORT_ID,
) -> dict[str, Any]:
    profile = _profile(export_id)
    consumers = dependency_snapshot.get("consumers")
    _require(isinstance(consumers, dict), "dependency snapshot tool versions are missing")
    seal_hashes = report.get("dependencies", {}).get("current_hashes")
    _require(isinstance(seal_hashes, dict), "seal tool hash map is missing")
    seal_tool_hash = seal_hashes.get("external/workflow_batch_seal.py")
    _require(_is_sha256(seal_tool_hash), "seal helper hash is invalid")
    ab_eval_version = consumers.get("ab_eval")
    normalizer_version = consumers.get("runtime_normalizer")
    _require(isinstance(ab_eval_version, str) and ab_eval_version, "ab_eval version is missing")
    _require(isinstance(normalizer_version, (str, int)) and not isinstance(normalizer_version, bool), "runtime normalizer version is missing")
    return {
        "schema_version": 1,
        "type": "workflow_batch_export_evidence",
        "export_id": export_id,
        "exporter": {"name": "eval.export_workflow_batch", "version": profile["version"]},
        "source_names": profile["source_names"],
        "source_hashes": {
            "seal_report_sha256": hashes["seal_report_sha256"],
            "ab_summary_sha256": hashes["ab_summary_sha256"],
            "dependency_snapshot_file_sha256": hashes["dependency_snapshot_file_sha256"],
            "plan_sha256": hashes["plan_sha256"],
            "runtime_config_sha256": hashes["runtime_config_sha256"],
        },
        "summary_binding": {
            "sha256": _sha256(summary_bytes),
            "projection_sha256": _sha256(_json_bytes(summary)),
        },
        "tool_versions": {
            "exporter": profile["version"],
            "ab_eval": ab_eval_version,
            "runtime_normalizer": normalizer_version,
            "workflow_seal_schema_version": report.get("schema_version"),
            "workflow_seal_helper_sha256": seal_tool_hash,
        },
    }


def _check_payload(
    summary_bytes: bytes,
    evidence_bytes: bytes,
    hashes: dict[str, str],
    export_id: str = EXPORT_ID,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "type": "workflow_batch_export_check",
        "export_id": export_id,
        "status": "passed",
        "checks": {
            "source_hashes": "verified",
            "summary_projection": "verified",
            "summary_file_bytes": "verified",
            "evidence_bindings": "verified",
        },
        "summary_sha256": _sha256(summary_bytes),
        "evidence_sha256": _sha256(evidence_bytes),
        "source_hashes": {
            "seal_report_sha256": hashes["seal_report_sha256"],
            "ab_summary_sha256": hashes["ab_summary_sha256"],
            "dependency_snapshot_file_sha256": hashes["dependency_snapshot_file_sha256"],
            "plan_sha256": hashes["plan_sha256"],
            "runtime_config_sha256": hashes["runtime_config_sha256"],
        },
    }


def _read_sources(
    seal_report_path: Path,
    ab_summary_path: Path,
    dependency_snapshot_path: Path,
    plan_path: Path,
    runtime_config_path: Path,
    expected_source_hashes: dict[str, str] | None = None,
    export_id: str = EXPORT_ID,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    try:
        values = {
            "seal_report": seal_report_path.read_bytes(),
            "ab_summary": ab_summary_path.read_bytes(),
            "dependency_snapshot": dependency_snapshot_path.read_bytes(),
            "plan": plan_path.read_bytes(),
            "runtime_config": runtime_config_path.read_bytes(),
        }
    except OSError as exc:
        raise ExportError("one or more source files cannot be read") from exc
    report = _load_json(values["seal_report"], "seal report")
    ab_summary = _load_json(values["ab_summary"], "ab_eval summary")
    dependency_snapshot = _load_json(values["dependency_snapshot"], "dependency snapshot")
    hashes = _source_hashes(
        values["seal_report"], values["ab_summary"], values["dependency_snapshot"],
        values["plan"], values["runtime_config"], expected_source_hashes, export_id,
    )
    return values, report, ab_summary, dependency_snapshot, hashes


def _project_sources(
    report: Any,
    ab_summary: Any,
    dependency_snapshot: Any,
    hashes: dict[str, str],
    *,
    plan_bytes: bytes | None = None,
    export_id: str = EXPORT_ID,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if export_id == EXPORT_ID:
        slots, pairs, audit = _validate_source_objects(report, ab_summary, dependency_snapshot, hashes)
        summary = _project_summary(report, ab_summary, slots, pairs, audit)
    elif export_id == V3_EXPORT_ID:
        _require(plan_bytes is not None, "v3 batch plan bytes are required")
        plan = _load_json(plan_bytes, "v3 batch plan")
        slots, pairs, audit = _validate_v3_source_objects(report, ab_summary, dependency_snapshot, plan, hashes)
        summary = _project_v3_summary(report, ab_summary, slots, pairs, audit)
    else:
        raise ExportError(f"unsupported export id: {export_id}")
    return summary, _build_evidence(
        summary, _json_bytes(summary), report, dependency_snapshot, hashes, export_id,
    )


def _safe_publish_new(payloads: list[tuple[Path, bytes]]) -> None:
    destinations = [path.resolve() for path, _ in payloads]
    _require(len(destinations) == len(set(destinations)), "output paths must be distinct")
    _require(not any(path.exists() for path in destinations), "refusing to overwrite an existing export file")
    staged: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for destination, payload in payloads:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o644)
            staged.append((temporary, destination))
        for temporary, destination in staged:
            os.link(temporary, destination)
            published.append(destination)
    except OSError as exc:
        for destination in published:
            destination.unlink(missing_ok=True)
        raise ExportError("could not publish workflow export files") from exc
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def _replace_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check_export(
    summary_bytes: bytes,
    evidence_bytes: bytes,
    *,
    seal_report_bytes: bytes,
    ab_summary_bytes: bytes,
    dependency_snapshot_bytes: bytes,
    plan_bytes: bytes,
    runtime_config_bytes: bytes,
    expected_source_hashes: dict[str, str] | None = None,
    export_id: str = EXPORT_ID,
) -> dict[str, Any]:
    hashes = _source_hashes(
        seal_report_bytes, ab_summary_bytes, dependency_snapshot_bytes,
        plan_bytes, runtime_config_bytes, expected_source_hashes, export_id,
    )
    report = _load_json(seal_report_bytes, "seal report")
    ab_summary = _load_json(ab_summary_bytes, "ab_eval summary")
    dependency_snapshot = _load_json(dependency_snapshot_bytes, "dependency snapshot")
    expected_summary, expected_evidence = _project_sources(
        report, ab_summary, dependency_snapshot, hashes,
        plan_bytes=plan_bytes, export_id=export_id,
    )
    _require(summary_bytes == _json_bytes(expected_summary), "summary bytes differ from the recomputed source projection")
    _require(evidence_bytes == _json_bytes(expected_evidence), "evidence bytes differ from recomputed source bindings")
    return _check_payload(summary_bytes, evidence_bytes, hashes, export_id)


def build_export(
    seal_report_path: Path,
    ab_summary_path: Path,
    dependency_snapshot_path: Path,
    plan_path: Path,
    runtime_config_path: Path,
    summary_output: Path,
    evidence_output: Path,
    check_output: Path,
    *,
    expected_source_hashes: dict[str, str] | None = None,
    export_id: str = EXPORT_ID,
) -> dict[str, Any]:
    values, report, ab_summary, dependency_snapshot, actual_hashes = _read_sources(
        seal_report_path, ab_summary_path, dependency_snapshot_path, plan_path, runtime_config_path,
        expected_source_hashes, export_id,
    )
    hashes = _source_hashes(
        values["seal_report"], values["ab_summary"], values["dependency_snapshot"],
        values["plan"], values["runtime_config"], expected_source_hashes, export_id,
    )
    _require(hashes == actual_hashes, "source hash computation was inconsistent")
    summary, _ = _project_sources(
        report, ab_summary, dependency_snapshot, hashes,
        plan_bytes=values["plan"], export_id=export_id,
    )
    summary_bytes = _json_bytes(summary)
    evidence = _build_evidence(summary, summary_bytes, report, dependency_snapshot, hashes, export_id)
    evidence_bytes = _json_bytes(evidence)
    check_value = check_export(
        summary_bytes, evidence_bytes,
        seal_report_bytes=values["seal_report"], ab_summary_bytes=values["ab_summary"],
        dependency_snapshot_bytes=values["dependency_snapshot"], plan_bytes=values["plan"],
        runtime_config_bytes=values["runtime_config"], expected_source_hashes=expected_source_hashes,
        export_id=export_id,
    )
    check_bytes = _json_bytes(check_value)
    _safe_publish_new([
        (summary_output, summary_bytes),
        (evidence_output, evidence_bytes),
        (check_output, check_bytes),
    ])
    return {"summary": summary, "evidence": evidence, "check": check_value}


def _validate_batch_id(batch_id: str) -> str:
    _require(
        isinstance(batch_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", batch_id) is not None,
        "batch id must be 1-64 safe ASCII letters, digits, dots, underscores, or hyphens",
    )
    return batch_id


def _closure_input_hashes(
    values: dict[str, bytes],
    *,
    ab_summary_present: bool,
    expected_closure_sha256: str | None,
    expected_post_snapshot_sha256: str | None,
) -> dict[str, str | None]:
    _require(_is_sha256(expected_closure_sha256), "expected closure SHA-256 pin is required")
    _require(_is_sha256(expected_post_snapshot_sha256), "expected post-snapshot SHA-256 pin is required")
    hashes: dict[str, str | None] = {
        "closure_report_sha256": _sha256(values["closure_report"]),
        "post_execution_snapshot_sha256": _sha256(values["post_execution_snapshot"]),
        "partial_workflow_report_sha256": _sha256(values["partial_workflow_report"]),
        "pre_execution_snapshot_sha256": _sha256(values["pre_execution_snapshot"]),
        "ab_summary_sha256": _sha256(values["ab_summary"]) if ab_summary_present else None,
        "plan_sha256": _sha256(values["plan"]),
        "runtime_config_sha256": _sha256(values["runtime_config"]),
    }
    _require(hashes["closure_report_sha256"] == expected_closure_sha256,
        "pinned closure report byte hash does not match")
    _require(hashes["post_execution_snapshot_sha256"] == expected_post_snapshot_sha256,
        "pinned post-execution snapshot byte hash does not match")
    return hashes


def _closure_projection(
    values: dict[str, bytes],
    source_names: dict[str, str | None],
    *,
    batch_id: str,
    ab_summary_present: bool,
    expected_closure_sha256: str | None,
    expected_post_snapshot_sha256: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str | None]]:
    batch_id = _validate_batch_id(batch_id)
    hashes = _closure_input_hashes(
        values,
        ab_summary_present=ab_summary_present,
        expected_closure_sha256=expected_closure_sha256,
        expected_post_snapshot_sha256=expected_post_snapshot_sha256,
    )
    _require(ab_summary_present == ("ab_summary" in values),
        "provide either a real ab_eval summary or --ab-summary-not-generated")
    closure = _load_json(values["closure_report"], "workflow closure report")
    post_snapshot = _load_json(values["post_execution_snapshot"], "post-execution snapshot")
    partial_report = _load_json(values["partial_workflow_report"], "partial workflow report")
    pre_snapshot = _load_json(values["pre_execution_snapshot"], "pre-execution snapshot")
    plan = _load_json(values["plan"], "batch plan")
    ab_summary = _load_json(values["ab_summary"], "ab_eval summary") if ab_summary_present else None

    for key, name in source_names.items():
        if name is not None:
            _require(
                isinstance(name, str) and name not in {"", ".", ".."}
                and Path(name).name == name and "\x00" not in name,
                f"{key} input name must be a basename",
            )
    _require(source_names.get("ab_summary") is not None if ab_summary_present else source_names.get("ab_summary") is None,
        "ab_eval summary source name does not match its generation state")

    slots, pairs, audit = _validate_closure_sources(
        closure, post_snapshot, partial_report, pre_snapshot, plan, ab_summary, hashes,
    )
    summary = _project_v3_summary(
        partial_report,
        ab_summary,
        slots,
        pairs,
        audit,
        export_id=batch_id,
        exporter_version=CLOSURE_EXPORTER_VERSION,
    )
    summary["execution_state"] = closure["execution_state"]
    summary_bytes = _json_bytes(summary)

    consumers = pre_snapshot.get("consumers")
    _require(isinstance(consumers, dict), "pre-execution snapshot tool versions are missing")
    dependencies = partial_report.get("dependencies")
    _require(isinstance(dependencies, dict), "partial workflow dependency state is missing")
    seal_hashes = dependencies.get("current_hashes")
    _require(isinstance(seal_hashes, dict), "partial workflow seal tool hash map is missing")
    seal_tool_hash = seal_hashes.get("external/workflow_batch_seal.py")
    _require(_is_sha256(seal_tool_hash), "partial workflow seal helper hash is invalid")
    evidence = {
        "schema_version": 1,
        "type": "workflow_batch_export_evidence",
        "export_id": batch_id,
        "exporter": {"name": "eval.export_workflow_batch", "version": CLOSURE_EXPORTER_VERSION},
        "source_names": source_names,
        "source_status": {"ab_summary": "provided" if ab_summary_present else "not_generated"},
        "source_hashes": hashes,
        "summary_binding": {
            "sha256": _sha256(summary_bytes),
            "projection_sha256": _sha256(_json_bytes(summary)),
        },
        "tool_versions": {
            "exporter": CLOSURE_EXPORTER_VERSION,
            "ab_eval": consumers.get("ab_eval"),
            "runtime_normalizer": consumers.get("runtime_normalizer"),
            "workflow_seal_schema_version": partial_report.get("schema_version"),
            "workflow_seal_helper_sha256": seal_tool_hash,
        },
    }
    _require(isinstance(evidence["tool_versions"]["ab_eval"], str)
        and evidence["tool_versions"]["ab_eval"], "ab_eval version is missing")
    _require(isinstance(evidence["tool_versions"]["runtime_normalizer"], (str, int))
        and not isinstance(evidence["tool_versions"]["runtime_normalizer"], bool),
        "runtime normalizer version is missing")
    return summary, evidence, hashes


def check_closure_export(
    summary_bytes: bytes,
    evidence_bytes: bytes,
    *,
    closure_report_bytes: bytes,
    post_execution_snapshot_bytes: bytes,
    partial_workflow_report_bytes: bytes,
    pre_execution_snapshot_bytes: bytes,
    plan_bytes: bytes,
    runtime_config_bytes: bytes,
    ab_summary_bytes: bytes | None = None,
    ab_summary_not_generated: bool = False,
    expected_closure_sha256: str | None = None,
    expected_post_snapshot_sha256: str | None = None,
    batch_id: str,
    source_names: dict[str, str | None],
) -> dict[str, Any]:
    _require((ab_summary_bytes is None) == ab_summary_not_generated,
        "select exactly one of a real ab_eval summary or --ab-summary-not-generated")
    values = {
        "closure_report": closure_report_bytes,
        "post_execution_snapshot": post_execution_snapshot_bytes,
        "partial_workflow_report": partial_workflow_report_bytes,
        "pre_execution_snapshot": pre_execution_snapshot_bytes,
        "plan": plan_bytes,
        "runtime_config": runtime_config_bytes,
    }
    if ab_summary_bytes is not None:
        values["ab_summary"] = ab_summary_bytes
    expected_summary, expected_evidence, hashes = _closure_projection(
        values,
        source_names,
        batch_id=batch_id,
        ab_summary_present=ab_summary_bytes is not None,
        expected_closure_sha256=expected_closure_sha256,
        expected_post_snapshot_sha256=expected_post_snapshot_sha256,
    )
    _require(summary_bytes == _json_bytes(expected_summary),
        "summary bytes differ from the recomputed closure projection")
    _require(evidence_bytes == _json_bytes(expected_evidence),
        "evidence bytes differ from recomputed closure source bindings")
    return {
        "schema_version": 1,
        "type": "workflow_batch_export_check",
        "export_id": batch_id,
        "status": "passed",
        "checks": {
            "source_hashes": "verified",
            "closure_bindings": "verified",
            "summary_projection": "verified",
            "summary_file_bytes": "verified",
            "evidence_bindings": "verified",
        },
        "summary_sha256": _sha256(summary_bytes),
        "evidence_sha256": _sha256(evidence_bytes),
        "source_hashes": hashes,
    }


def build_closure_export(
    closure_report_path: Path,
    post_execution_snapshot_path: Path,
    partial_workflow_report_path: Path,
    pre_execution_snapshot_path: Path,
    plan_path: Path,
    runtime_config_path: Path,
    summary_output: Path,
    evidence_output: Path,
    check_output: Path,
    *,
    ab_summary_path: Path | None = None,
    ab_summary_not_generated: bool = False,
    expected_closure_sha256: str | None = None,
    expected_post_snapshot_sha256: str | None = None,
    batch_id: str,
) -> dict[str, Any]:
    _require((ab_summary_path is None) == ab_summary_not_generated,
        "select exactly one of a real ab_eval summary or --ab-summary-not-generated")
    paths = {
        "closure_report": closure_report_path,
        "post_execution_snapshot": post_execution_snapshot_path,
        "partial_workflow_report": partial_workflow_report_path,
        "pre_execution_snapshot": pre_execution_snapshot_path,
        "plan": plan_path,
        "runtime_config": runtime_config_path,
    }
    if ab_summary_path is not None:
        paths["ab_summary"] = ab_summary_path
    try:
        values = {name: path.read_bytes() for name, path in paths.items()}
    except OSError as exc:
        raise ExportError("one or more closure source files cannot be read") from exc
    source_names = {name: path.name for name, path in paths.items()}
    source_names.setdefault("ab_summary", None)
    summary, evidence, _hashes = _closure_projection(
        values,
        source_names,
        batch_id=batch_id,
        ab_summary_present=ab_summary_path is not None,
        expected_closure_sha256=expected_closure_sha256,
        expected_post_snapshot_sha256=expected_post_snapshot_sha256,
    )
    summary_bytes = _json_bytes(summary)
    evidence_bytes = _json_bytes(evidence)
    check_value = check_closure_export(
        summary_bytes,
        evidence_bytes,
        closure_report_bytes=values["closure_report"],
        post_execution_snapshot_bytes=values["post_execution_snapshot"],
        partial_workflow_report_bytes=values["partial_workflow_report"],
        pre_execution_snapshot_bytes=values["pre_execution_snapshot"],
        plan_bytes=values["plan"],
        runtime_config_bytes=values["runtime_config"],
        ab_summary_bytes=values.get("ab_summary"),
        ab_summary_not_generated=ab_summary_not_generated,
        expected_closure_sha256=expected_closure_sha256,
        expected_post_snapshot_sha256=expected_post_snapshot_sha256,
        batch_id=batch_id,
        source_names=source_names,
    )
    _safe_publish_new([
        (summary_output, summary_bytes),
        (evidence_output, evidence_bytes),
        (check_output, _json_bytes(check_value)),
    ])
    return {"summary": summary, "evidence": evidence, "check": check_value}


def _closure_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--closure-report", type=Path, required=True)
    parser.add_argument("--post-snapshot", type=Path, required=True)
    parser.add_argument("--partial-report", type=Path, required=True)
    parser.add_argument("--pre-snapshot", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--expected-closure-sha256", required=True)
    parser.add_argument("--expected-post-snapshot-sha256", required=True)
    summary = parser.add_mutually_exclusive_group(required=True)
    summary.add_argument("--ab-summary", type=Path)
    summary.add_argument("--ab-summary-not-generated", action="store_true")


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--export-id", choices=(EXPORT_ID, V3_EXPORT_ID), default=EXPORT_ID)
    parser.add_argument("--seal-report", type=Path, required=True)
    parser.add_argument("--ab-summary", type=Path, required=True)
    parser.add_argument("--dependency-snapshot", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build summary, evidence, and a successful verification record")
    _source_arguments(build)
    build.add_argument("--summary-output", type=Path, required=True)
    build.add_argument("--evidence-output", type=Path, required=True)
    build.add_argument("--check-output", type=Path, required=True)
    check = commands.add_parser("check", help="Recompute the projection and source hashes")
    _source_arguments(check)
    check.add_argument("--summary", type=Path, required=True)
    check.add_argument("--evidence", type=Path, required=True)
    check.add_argument("--check-output", type=Path, required=True)

    build_closure = commands.add_parser(
        "build-closure", help="Build an export from a hash-pinned closed workflow failure report",
    )
    _closure_source_arguments(build_closure)
    build_closure.add_argument("--summary-output", type=Path, required=True)
    build_closure.add_argument("--evidence-output", type=Path, required=True)
    build_closure.add_argument("--check-output", type=Path, required=True)

    check_closure = commands.add_parser(
        "check-closure", help="Recompute a hash-pinned closure export",
    )
    _closure_source_arguments(check_closure)
    check_closure.add_argument("--summary", type=Path, required=True)
    check_closure.add_argument("--evidence", type=Path, required=True)
    check_closure.add_argument("--check-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            value = build_export(
                args.seal_report, args.ab_summary, args.dependency_snapshot, args.plan,
                args.runtime_config, args.summary_output, args.evidence_output, args.check_output,
                export_id=args.export_id,
            )
            print(json.dumps({
                "summary": str(args.summary_output),
                "evidence": str(args.evidence_output),
                "check": str(args.check_output),
                "status": value["check"]["status"],
                "summary_sha256": value["check"]["summary_sha256"],
            }, sort_keys=True))
            return 0

        if args.command == "build-closure":
            value = build_closure_export(
                args.closure_report,
                args.post_snapshot,
                args.partial_report,
                args.pre_snapshot,
                args.plan,
                args.runtime_config,
                args.summary_output,
                args.evidence_output,
                args.check_output,
                ab_summary_path=args.ab_summary,
                ab_summary_not_generated=args.ab_summary_not_generated,
                expected_closure_sha256=args.expected_closure_sha256,
                expected_post_snapshot_sha256=args.expected_post_snapshot_sha256,
                batch_id=args.batch_id,
            )
            print(json.dumps({
                "summary": str(args.summary_output),
                "evidence": str(args.evidence_output),
                "check": str(args.check_output),
                "status": value["check"]["status"],
                "summary_sha256": value["check"]["summary_sha256"],
            }, sort_keys=True))
            return 0

        if args.command == "check-closure":
            source_paths = {
                "closure_report": args.closure_report,
                "post_execution_snapshot": args.post_snapshot,
                "partial_workflow_report": args.partial_report,
                "pre_execution_snapshot": args.pre_snapshot,
                "plan": args.plan,
                "runtime_config": args.runtime_config,
            }
            if args.ab_summary is not None:
                source_paths["ab_summary"] = args.ab_summary
            source_names = {name: path.name for name, path in source_paths.items()}
            source_names.setdefault("ab_summary", None)
            try:
                values = {name: path.read_bytes() for name, path in source_paths.items()}
                result = check_closure_export(
                    args.summary.read_bytes(),
                    args.evidence.read_bytes(),
                    closure_report_bytes=values["closure_report"],
                    post_execution_snapshot_bytes=values["post_execution_snapshot"],
                    partial_workflow_report_bytes=values["partial_workflow_report"],
                    pre_execution_snapshot_bytes=values["pre_execution_snapshot"],
                    plan_bytes=values["plan"],
                    runtime_config_bytes=values["runtime_config"],
                    ab_summary_bytes=values.get("ab_summary"),
                    ab_summary_not_generated=args.ab_summary_not_generated,
                    expected_closure_sha256=args.expected_closure_sha256,
                    expected_post_snapshot_sha256=args.expected_post_snapshot_sha256,
                    batch_id=args.batch_id,
                    source_names=source_names,
                )
            except OSError as exc:
                raise ExportError("one or more closure export or source files cannot be read") from exc
            _safe_publish_new([(args.check_output, _json_bytes(result))])
            print(json.dumps({"check": str(args.check_output), "status": result["status"]}, sort_keys=True))
            return 0

        values, _, _, _, _ = _read_sources(
            args.seal_report, args.ab_summary, args.dependency_snapshot, args.plan, args.runtime_config,
            export_id=args.export_id,
        )
        result = check_export(
            args.summary.read_bytes(), args.evidence.read_bytes(),
            seal_report_bytes=values["seal_report"], ab_summary_bytes=values["ab_summary"],
            dependency_snapshot_bytes=values["dependency_snapshot"], plan_bytes=values["plan"],
            runtime_config_bytes=values["runtime_config"],
            export_id=args.export_id,
        )
        _replace_json(args.check_output, result)
        print(json.dumps({"check": str(args.check_output), "status": result["status"]}, sort_keys=True))
        return 0
    except (OSError, ExportError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        if args.command == "check":
            _replace_json(args.check_output, {
                "schema_version": 1,
                "type": "workflow_batch_export_check",
                "export_id": args.export_id,
                "status": "rejected",
                "error": str(exc),
            })
        print(f"export_workflow_batch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
