#!/usr/bin/env python3
"""Export and verify a privacy-limited projection of a sealed workflow batch."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


VERSION = "1.0.0"
EXPORT_ID = "2026-10-12-ab-workflow-v2"
EXPECTED_SOURCE_SHA256 = {
    "seal_report": "d4b906458da7d58b52472beed0b4eca53bb4d45bcb6965eac26580e8523faba3",
    "ab_summary": "27fff54bb1527104c043ad00b99c0c2eebadef4b2dc1e3d7a22b14ea444b6654",
}
CONDITION_NAMES = {"A": "same_host_direct", "B": "current_skill"}
HEX_DIGITS = frozenset("0123456789abcdef")


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


def _source_hashes(
    seal_report_bytes: bytes,
    ab_summary_bytes: bytes,
    dependency_snapshot_bytes: bytes,
    plan_bytes: bytes,
    runtime_config_bytes: bytes,
    expected_source_hashes: dict[str, str] | None,
) -> dict[str, str]:
    hashes = {
        "seal_report_sha256": _sha256(seal_report_bytes),
        "ab_summary_sha256": _sha256(ab_summary_bytes),
        "dependency_snapshot_file_sha256": _sha256(dependency_snapshot_bytes),
        "plan_sha256": _sha256(plan_bytes),
        "runtime_config_sha256": _sha256(runtime_config_bytes),
    }
    expected = EXPECTED_SOURCE_SHA256 if expected_source_hashes is None else expected_source_hashes
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


def _build_evidence(
    summary: dict[str, Any],
    summary_bytes: bytes,
    report: dict[str, Any],
    dependency_snapshot: dict[str, Any],
    hashes: dict[str, str],
) -> dict[str, Any]:
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
        "export_id": EXPORT_ID,
        "exporter": {"name": "eval.export_workflow_batch", "version": VERSION},
        "source_names": {
            "seal_report": "workflow-report-v2.json",
            "ab_summary": "ab-summary-final-v2.json",
            "dependency_snapshot": "dependencies-before-v2.json",
        },
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
            "exporter": VERSION,
            "ab_eval": ab_eval_version,
            "runtime_normalizer": normalizer_version,
            "workflow_seal_schema_version": report.get("schema_version"),
            "workflow_seal_helper_sha256": seal_tool_hash,
        },
    }


def _check_payload(summary_bytes: bytes, evidence_bytes: bytes, hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "type": "workflow_batch_export_check",
        "export_id": EXPORT_ID,
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
        values["plan"], values["runtime_config"], expected_source_hashes,
    )
    return values, report, ab_summary, dependency_snapshot, hashes


def _project_sources(
    report: Any,
    ab_summary: Any,
    dependency_snapshot: Any,
    hashes: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    slots, pairs, audit = _validate_source_objects(report, ab_summary, dependency_snapshot, hashes)
    summary = _project_summary(report, ab_summary, slots, pairs, audit)
    return summary, _build_evidence(summary, _json_bytes(summary), report, dependency_snapshot, hashes)


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
) -> dict[str, Any]:
    hashes = _source_hashes(
        seal_report_bytes, ab_summary_bytes, dependency_snapshot_bytes,
        plan_bytes, runtime_config_bytes, expected_source_hashes,
    )
    report = _load_json(seal_report_bytes, "seal report")
    ab_summary = _load_json(ab_summary_bytes, "ab_eval summary")
    dependency_snapshot = _load_json(dependency_snapshot_bytes, "dependency snapshot")
    expected_summary, expected_evidence = _project_sources(report, ab_summary, dependency_snapshot, hashes)
    _require(summary_bytes == _json_bytes(expected_summary), "summary bytes differ from the recomputed source projection")
    _require(evidence_bytes == _json_bytes(expected_evidence), "evidence bytes differ from recomputed source bindings")
    return _check_payload(summary_bytes, evidence_bytes, hashes)


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
) -> dict[str, Any]:
    values, report, ab_summary, dependency_snapshot, actual_hashes = _read_sources(
        seal_report_path, ab_summary_path, dependency_snapshot_path, plan_path, runtime_config_path,
        expected_source_hashes,
    )
    hashes = _source_hashes(
        values["seal_report"], values["ab_summary"], values["dependency_snapshot"],
        values["plan"], values["runtime_config"], expected_source_hashes,
    )
    _require(hashes == actual_hashes, "source hash computation was inconsistent")
    summary, _ = _project_sources(report, ab_summary, dependency_snapshot, hashes)
    summary_bytes = _json_bytes(summary)
    evidence = _build_evidence(summary, summary_bytes, report, dependency_snapshot, hashes)
    evidence_bytes = _json_bytes(evidence)
    check_value = check_export(
        summary_bytes, evidence_bytes,
        seal_report_bytes=values["seal_report"], ab_summary_bytes=values["ab_summary"],
        dependency_snapshot_bytes=values["dependency_snapshot"], plan_bytes=values["plan"],
        runtime_config_bytes=values["runtime_config"], expected_source_hashes=expected_source_hashes,
    )
    check_bytes = _json_bytes(check_value)
    _safe_publish_new([
        (summary_output, summary_bytes),
        (evidence_output, evidence_bytes),
        (check_output, check_bytes),
    ])
    return {"summary": summary, "evidence": evidence, "check": check_value}


def _source_arguments(parser: argparse.ArgumentParser) -> None:
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
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            value = build_export(
                args.seal_report, args.ab_summary, args.dependency_snapshot, args.plan,
                args.runtime_config, args.summary_output, args.evidence_output, args.check_output,
            )
            print(json.dumps({
                "summary": str(args.summary_output),
                "evidence": str(args.evidence_output),
                "check": str(args.check_output),
                "status": value["check"]["status"],
                "summary_sha256": value["check"]["summary_sha256"],
            }, sort_keys=True))
            return 0

        values, _, _, _, _ = _read_sources(
            args.seal_report, args.ab_summary, args.dependency_snapshot, args.plan, args.runtime_config,
        )
        result = check_export(
            args.summary.read_bytes(), args.evidence.read_bytes(),
            seal_report_bytes=values["seal_report"], ab_summary_bytes=values["ab_summary"],
            dependency_snapshot_bytes=values["dependency_snapshot"], plan_bytes=values["plan"],
            runtime_config_bytes=values["runtime_config"],
        )
        _replace_json(args.check_output, result)
        print(json.dumps({"check": str(args.check_output), "status": result["status"]}, sort_keys=True))
        return 0
    except (OSError, ExportError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        if args.command == "check":
            _replace_json(args.check_output, {
                "schema_version": 1,
                "type": "workflow_batch_export_check",
                "export_id": EXPORT_ID,
                "status": "rejected",
                "error": str(exc),
            })
        print(f"export_workflow_batch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
