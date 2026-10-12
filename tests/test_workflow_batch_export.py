from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import eval.export_workflow_batch as exporter


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(tmp_path: Path) -> dict[str, Path | bytes | dict[str, str]]:
    snapshot = {
        "schema_version": 1,
        "type": "pre_execution_snapshot",
        "benchmark_id": "2026-10-10-ab-workflow-validation",
        "consumers": {"ab_eval": "0.2.0", "runtime_normalizer": "2"},
    }
    snapshot_bytes = _json_bytes(snapshot)
    plan_bytes = b"synthetic plan bytes; not exported\n"
    config_bytes = b'{"api_key":"DO_NOT_EXPORT_CONFIG"}\n'
    slots = []
    planned_order = (
        ("case0002", "A", 1), ("case0003", "B", 1), ("case0006", "A", 1),
        ("case0003", "A", 1), ("case0002", "B", 1), ("case0006", "B", 1),
        ("case0002", "B", 2), ("case0003", "A", 2), ("case0006", "B", 2),
        ("case0003", "B", 2), ("case0002", "A", 2), ("case0006", "A", 2),
    )
    for order, (case_id, condition, replicate) in enumerate(planned_order, start=1):
        failed = order == 1
        slots.append({
            "slot_id": f"{case_id}:{condition}:replicate-{replicate}",
            "case_id": case_id,
            "condition": condition,
            "replicate": replicate,
            "run_order": order,
            "planned": True,
            "started": failed,
            "host_completed": False,
            "evidence_valid": False,
            "status": "adapter_failure_without_slot_result" if failed else "not_started",
            "sealed_artifact_check": {
                "status": "not_applicable",
                "references_checked": 0,
                "mismatches": [],
            },
            "artifacts": {"raw_trace": {"path": "runtime/strace.log", "sha256": "a" * 64}},
        })
    pairs = [
        {
            "case_id": case_id,
            "replicate": replicate,
            "ab_eval_status": "pair_incomplete_missing_run_record",
            "eligible": False,
            "slots": [f"{case_id}:A:replicate-{replicate}", f"{case_id}:B:replicate-{replicate}"],
        }
        for case_id in ("case0002", "case0003", "case0006")
        for replicate in (1, 2)
    ]
    report = {
        "schema_version": 1,
        "type": "workflow_only_batch_seal",
        "seal_status": "accepted",
        "benchmark_id": "2026-10-10-ab-workflow-validation",
        "plan_sha256": _sha(plan_bytes),
        "runtime_config_sha256": _sha(config_bytes),
        "prepared_input_scope": {
            "purpose": "workflow_reproducibility_validation_of_existing_corpus",
            "fresh_blind_test": False,
            "description": "Prepared inputs from the existing corpus; workflow reproducibility only, not a fresh blind quality benchmark.",
        },
        "dependencies": {
            "status": "unchanged",
            "changed_paths": [],
            "plan_changed": False,
            "runtime_config_changed": False,
            "pre_execution_snapshot_sha256": _sha(snapshot_bytes),
            "current_hashes": {"external/workflow_batch_seal.py": "b" * 64},
        },
        "sealed_artifacts": {
            "status": "no_mismatches",
            "references_checked": 0,
            "unavailable_slot_results": 12,
            "mismatches": [],
        },
        "counts": {
            "planned_slots": 12,
            "started_slots": 1,
            "host_completed_slots": 0,
            "evidence_valid_slots": 0,
            "eligible_pairs": 0,
            "planned_pairs": 6,
        },
        "slots": slots,
        "pairs": pairs,
        "ab_eval": {"status": "not_generated", "pairs": []},
        "human_quality": {
            "annotation_status": "pending",
            "human_labels_read": False,
            "precision": None,
            "recall": None,
            "clean_case_false_alarms": None,
            "wrong_resolution_count": None,
        },
    }
    ab_summary = {
        "schema_version": 1,
        "run_count": 0,
        "adjudicated_case_count": 0,
        "pending_annotation_case_count": 0,
        "private_output": "DO_NOT_EXPORT_SUMMARY",
    }
    source_bytes = {
        "seal_report": _json_bytes(report),
        "ab_summary": _json_bytes(ab_summary),
        "dependency_snapshot": snapshot_bytes,
        "plan": plan_bytes,
        "runtime_config": config_bytes,
    }
    paths: dict[str, Path | bytes | dict[str, str]] = {}
    filenames = {
        "seal_report": "workflow-report-v2.json",
        "ab_summary": "ab-summary-final-v2.json",
        "dependency_snapshot": "dependencies-before-v2.json",
        "plan": "batch-plan.json",
        "runtime_config": "runtime-config.json",
    }
    for name, filename in filenames.items():
        path = tmp_path / filename
        path.write_bytes(source_bytes[name])
        paths[name] = path
    paths["expected_source_hashes"] = {
        "seal_report": _sha(source_bytes["seal_report"]),
        "ab_summary": _sha(source_bytes["ab_summary"]),
    }
    return paths


def _build(tmp_path: Path, sources: dict[str, Path | bytes | dict[str, str]]) -> tuple[Path, Path, Path]:
    output = tmp_path / "export"
    summary_path = output / f"{exporter.EXPORT_ID}-summary.json"
    evidence_path = output / f"{exporter.EXPORT_ID}-evidence.json"
    check_path = output / f"{exporter.EXPORT_ID}-check.json"
    exporter.build_export(
        sources["seal_report"],
        sources["ab_summary"],
        sources["dependency_snapshot"],
        sources["plan"],
        sources["runtime_config"],
        summary_path,
        evidence_path,
        check_path,
        expected_source_hashes=sources["expected_source_hashes"],
    )
    return summary_path, evidence_path, check_path


def test_build_projects_all_slots_and_binds_only_hashes(tmp_path: Path) -> None:
    sources = _fixture(tmp_path)
    summary_path, evidence_path, check_path = _build(tmp_path, sources)
    summary_bytes = summary_path.read_bytes()
    evidence_bytes = evidence_path.read_bytes()
    summary = json.loads(summary_bytes)
    evidence = json.loads(evidence_bytes)
    check = json.loads(check_path.read_bytes())

    assert check["status"] == "passed"
    assert check["summary_sha256"] == _sha(summary_bytes)
    assert evidence["summary_binding"]["sha256"] == _sha(summary_bytes)
    assert evidence["source_hashes"]["seal_report_sha256"] == _sha(sources["seal_report"].read_bytes())
    assert evidence["source_hashes"]["plan_sha256"] == _sha(sources["plan"].read_bytes())
    assert summary["workflow"]["planned_slots"] == 12
    assert summary["workflow"]["launched_slots"] == 1
    assert summary["workflow"]["host_completed_slots"] == 0
    assert summary["workflow"]["evidence_valid_slots"] == 0
    assert summary["workflow"]["failed_slots"] == 1
    assert summary["workflow"]["not_started_slots"] == 11
    assert len(summary["workflow"]["slots"]) == 12
    assert len(summary["workflow"]["pairs"]) == 6
    assert {pair["status"] for pair in summary["workflow"]["pairs"]} == {"pair_incomplete_missing_run_record"}
    assert summary["integrity"]["seal_status"] == "accepted"
    assert summary["integrity"]["seal_completeness"] == "incomplete"
    assert summary["integrity"]["artifact_reference_check"] == {
        "reported_status": "no_mismatches",
        "references_checked": 0,
        "unavailable_slot_results": 12,
    }
    assert summary["strict_ab_eval"] == {
        "run_records_created": 0,
        "build_status": "not_generated",
        "validate_status": "not_applicable",
        "score_status": "not_applicable",
    }
    assert summary["human_adjudication"]["labels"] == []
    assert summary["human_adjudication"]["precision"] is None
    assert summary["human_adjudication"]["recall"] is None
    assert summary["model_cost"] == {"status": "unknown", "amount": None, "currency": None}
    assert summary["execution_identity"] == {
        "status": "unknown", "provider": None, "model": None, "host": None,
    }
    published = summary_bytes + evidence_bytes + check_path.read_bytes()
    assert b"DO_NOT_EXPORT" not in published
    assert b"runtime/strace.log" not in published
    assert b"api_key" not in published


def test_check_rejects_summary_or_evidence_tampering(tmp_path: Path) -> None:
    sources = _fixture(tmp_path)
    summary_path, evidence_path, _ = _build(tmp_path, sources)
    source_bytes = {name: value.read_bytes() for name, value in sources.items() if isinstance(value, Path)}

    changed_summary = json.loads(summary_path.read_bytes())
    changed_summary["workflow"]["not_started_slots"] = 10
    with pytest.raises(exporter.ExportError, match="summary bytes differ"):
        exporter.check_export(
            _json_bytes(changed_summary), evidence_path.read_bytes(),
            seal_report_bytes=source_bytes["seal_report"],
            ab_summary_bytes=source_bytes["ab_summary"],
            dependency_snapshot_bytes=source_bytes["dependency_snapshot"],
            plan_bytes=source_bytes["plan"],
            runtime_config_bytes=source_bytes["runtime_config"],
            expected_source_hashes=sources["expected_source_hashes"],
        )

    changed_evidence = json.loads(evidence_path.read_bytes())
    changed_evidence["source_hashes"]["plan_sha256"] = "0" * 64
    with pytest.raises(exporter.ExportError, match="evidence bytes differ"):
        exporter.check_export(
            summary_path.read_bytes(), _json_bytes(changed_evidence),
            seal_report_bytes=source_bytes["seal_report"],
            ab_summary_bytes=source_bytes["ab_summary"],
            dependency_snapshot_bytes=source_bytes["dependency_snapshot"],
            plan_bytes=source_bytes["plan"],
            runtime_config_bytes=source_bytes["runtime_config"],
            expected_source_hashes=sources["expected_source_hashes"],
        )


def test_check_cli_replaces_pass_with_rejected_report_after_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _fixture(tmp_path)
    summary_path, evidence_path, check_path = _build(tmp_path, sources)
    monkeypatch.setattr(exporter, "EXPECTED_SOURCE_SHA256", sources["expected_source_hashes"])
    sources["plan"].write_bytes(sources["plan"].read_bytes() + b"tamper\n")
    result = exporter.main([
        "check",
        "--seal-report", str(sources["seal_report"]),
        "--ab-summary", str(sources["ab_summary"]),
        "--dependency-snapshot", str(sources["dependency_snapshot"]),
        "--plan", str(sources["plan"]),
        "--runtime-config", str(sources["runtime_config"]),
        "--summary", str(summary_path),
        "--evidence", str(evidence_path),
        "--check-output", str(check_path),
    ])

    assert result == 2
    rejected = json.loads(check_path.read_bytes())
    assert rejected["status"] == "rejected"
    assert "plan bytes do not match" in rejected["error"]
