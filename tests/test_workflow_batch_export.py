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


def _v3_fixture(tmp_path: Path, mode: str = "partial") -> dict[str, Path | bytes]:
    plan = {
        "schema_version": 1,
        "benchmark_id": exporter.V3_BENCHMARK_ID,
        "purpose": "workflow_reproducibility_validation_of_existing_corpus",
        "fresh_blind_test": False,
        "plan_state": "go",
        "execution_authorized": True,
        "annotation_status": "pending",
        "quality_metrics_separate": True,
        "conditions": {
            "A": "same_host_direct",
            "B": "same_host_direct_plus_pinned_unreleased_skill_source_snapshot",
        },
        "cases": {case_id: {"case_id": case_id} for case_id in ("case0002", "case0003", "case0006")},
        "slots": [
            {
                "case_id": case_id,
                "condition": condition,
                "replicate": replicate,
                "run_order": order,
                "attempt": 1,
                "attempt_kind": "independent_replicate",
            }
            for order, (case_id, condition, replicate) in enumerate(exporter.V3_SLOT_ORDER, start=1)
        ],
    }
    immutable_plan = {key: value for key, value in plan.items() if key not in {"plan_state", "execution_authorized"}}
    immutable_sha = _sha((json.dumps(
        immutable_plan, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8"))
    plan_bytes = _json_bytes(plan)
    runtime_bytes = b'{"config":"synthetic"}\n'
    snapshot = {
        "schema_version": 1,
        "type": "pre_execution_snapshot",
        "benchmark_id": exporter.V3_BENCHMARK_ID,
        "source_commit": "e8c719b635067ac33cca7300ffe8a4584ded3961",
        "plan_sha256": "f" * 64,
        "immutable_plan_sha256": immutable_sha,
        "consumers": {"ab_eval": "0.2.0", "runtime_normalizer": "3"},
        "slots": [
            {
                "slot_id": f"{case_id}:{condition}:replicate-{replicate}",
                "immutable_plan_sha256": immutable_sha,
                "source_bundle_manifest_sha256": "c" * 64,
                "manifest_file_count": 1,
            }
            for case_id, condition, replicate in exporter.V3_SLOT_ORDER
        ],
    }
    snapshot_bytes = _json_bytes(snapshot)
    artifact_names = {
        "slot_result", "run_record", "execution", "adapter_start", "adapter_failure",
        "codex_events", "raw_output", "host_output", "raw_trace", "supervisor_events",
        "source_bundle_manifest", "runtime_evidence",
    }

    def ref(name: str, digest: str = "a" * 64) -> dict[str, object]:
        return {"path": f"artifacts/{name}", "sha256": digest, "bytes": 17}

    full = mode == "complete"
    blocked = mode == "blocked"
    slots = []
    for order, (case_id, condition, replicate) in enumerate(exporter.V3_SLOT_ORDER, start=1):
        started = full or (mode in {"partial", "blocked"} and order == 1)
        host_completed = full or (blocked and started)
        evidence_valid = full or (blocked and started)
        artifacts = {name: None for name in artifact_names}
        artifacts["source_bundle_manifest"] = ref("source-bundle-manifest.json", "c" * 64)
        if full or (blocked and started) or (mode == "partial" and started):
            artifacts["slot_result"] = ref("slot-result.json")
        if full or (blocked and started):
            artifacts["run_record"] = ref("run.json")
        if mode == "partial" and started:
            artifacts["raw_trace"] = ref("strace.log")
        slots.append({
            "slot_id": f"{case_id}:{condition}:replicate-{replicate}",
            "case_id": case_id,
            "condition": condition,
            "replicate": replicate,
            "run_order": order,
            "planned": True,
            "started": started,
            "host_completed": host_completed,
            "evidence_valid": evidence_valid,
            "status": "completed" if full or (blocked and started) else "failed" if started else "not_started",
            "preseal_manifest_matches_snapshot": True,
            "source_bundle_manifest_sha256": "c" * 64,
            "source_bundle_check": {"status": "verified", "files_checked": 1, "mismatches": []},
            "sealed_artifact_check": {
                "status": "verified" if started else "not_applicable",
                "references_checked": (1 if full or blocked else 98) if started else 0,
                "mismatches": [],
            },
            "artifacts": artifacts,
        })

    strict_pairs = []
    seal_pairs = []
    for case_id in ("case0002", "case0003", "case0006"):
        for replicate in (1, 2):
            status = exporter.V3_ELIGIBLE_PAIR_STATUS if full else "pair_incomplete_missing_run_record"
            strict_pairs.append({
                "case_id": case_id,
                "replicate": replicate,
                "status": status,
                "human_precision": {"same_host_direct": None, "current_skill": None},
                "known_defect_recall": {"same_host_direct": None, "current_skill": None},
            })
            seal_pairs.append({
                "case_id": case_id,
                "replicate": replicate,
                "ab_eval_status": status,
                "eligible": full,
                "slots": [
                    f"{case_id}:A:replicate-{replicate}",
                    f"{case_id}:B:replicate-{replicate}",
                ],
            })
    run_count = 12 if full else (1 if blocked else 0)
    started_count = 12 if full else (1 if mode in {"partial", "blocked"} else 0)
    report = {
        "schema_version": 1,
        "type": "workflow_only_batch_seal",
        "seal_status": "accepted",
        "benchmark_id": exporter.V3_BENCHMARK_ID,
        "source_commit": snapshot["source_commit"],
        "plan_sha256": _sha(plan_bytes),
        "immutable_plan_sha256": immutable_sha,
        "runtime_config_sha256": _sha(runtime_bytes),
        "prepared_input_scope": {
            "purpose": "workflow_reproducibility_validation_of_existing_corpus",
            "fresh_blind_test": False,
            "description": "Prepared inputs from the existing corpus; workflow reproducibility only.",
        },
        "dependencies": {
            "status": "unchanged",
            "changed_paths": [],
            "plan_changed": False,
            "source_commit_changed": False,
            "runtime_config_changed": False,
            "pre_execution_snapshot_sha256": _sha(snapshot_bytes),
            "pre_execution_plan_sha256": snapshot["plan_sha256"],
            "current_hashes": {"external/workflow_batch_seal.py": "b" * 64},
        },
        "sealed_artifacts": {
            "status": "no_mismatches",
            "references_checked": 12 if full else (98 if mode == "partial" else 1 if blocked else 0),
            "unavailable_slot_results": 0 if full else (11 if mode in {"partial", "blocked"} else 12),
            "mismatches": [],
        },
        "counts": {
            "planned_slots": 12,
            "started_slots": started_count,
            "host_completed_slots": 12 if full else (1 if blocked else 0),
            "evidence_valid_slots": 12 if full else (1 if blocked else 0),
            "eligible_pairs": 6 if full else 0,
            "planned_pairs": 6,
        },
        "slots": slots,
        "pairs": seal_pairs,
        "ab_eval": {
            "status": "validated" if full else "blocked_label_reference_present" if blocked else "not_generated",
            "pairs": [
                {"case_id": pair["case_id"], "replicate": pair["replicate"], "status": pair["status"]}
                for pair in strict_pairs
            ] if full else [],
        },
        "human_quality": {
            "annotation_status": "pending",
            "human_labels_read": False,
            "precision": None,
            "recall": None,
            "clean_case_false_alarms": None,
            "wrong_resolution_count": None,
        },
        "model_cost": {"status": "unknown", "amount": None, "currency": None},
    }
    by_condition = {
        condition: {
            "human_precision_mean": None,
            "known_defect_recall_mean": None,
            "human_precision_case_count": 0,
            "known_defect_recall_case_count": 0,
        }
        for condition in exporter.CONDITION_NAMES.values()
    }
    ab_summary = {
        "schema_version": 1,
        "benchmark_id": exporter.V3_BENCHMARK_ID if run_count else None,
        "run_count": run_count,
        "adjudicated_case_count": 0,
        "pending_annotation_case_count": 3 if full else (1 if blocked else 0),
        "by_condition": by_condition,
        "paired_cases": strict_pairs if full else [],
        "private_output": "DO_NOT_EXPORT_V3",
    }
    source_bytes = {
        "seal_report": _json_bytes(report),
        "ab_summary": _json_bytes(ab_summary),
        "dependency_snapshot": snapshot_bytes,
        "plan": plan_bytes,
        "runtime_config": runtime_bytes,
    }
    filenames = {
        "seal_report": "workflow-report-v3.json",
        "ab_summary": "ab-summary-final-v3.json",
        "dependency_snapshot": "dependencies-before-v3.json",
        "plan": "batch-plan-v3.json",
        "runtime_config": "runtime-config-v3.json",
    }
    paths: dict[str, Path | bytes] = {}
    for name, filename in filenames.items():
        path = tmp_path / filename
        path.write_bytes(source_bytes[name])
        paths[name] = path
    return paths


def _build_v3(tmp_path: Path, sources: dict[str, Path | bytes]) -> tuple[Path, Path, Path]:
    output = tmp_path / "v3-export"
    summary_path = output / f"{exporter.V3_EXPORT_ID}-summary.json"
    evidence_path = output / f"{exporter.V3_EXPORT_ID}-evidence.json"
    check_path = output / f"{exporter.V3_EXPORT_ID}-check.json"
    exporter.build_export(
        sources["seal_report"],
        sources["ab_summary"],
        sources["dependency_snapshot"],
        sources["plan"],
        sources["runtime_config"],
        summary_path,
        evidence_path,
        check_path,
        export_id=exporter.V3_EXPORT_ID,
    )
    return summary_path, evidence_path, check_path


def _closure_record_reference(
    slot: dict[str, object],
    file_name: str,
    digest: str,
    byte_count: int,
) -> dict[str, object]:
    case_id = slot["case_id"]
    condition = slot["condition"]
    replicate = slot["replicate"]
    slot_id = slot["slot_id"]
    return {
        "identity": {
            "benchmark_id": exporter.V3_BENCHMARK_ID,
            "case_id": case_id,
            "condition": condition,
            "replicate": replicate,
            "slot_id": slot_id,
        },
        "source": f"slots/{case_id}/{condition}/repeat{replicate:02d}/{file_name}",
        "sha256": digest,
        "bytes": byte_count,
    }


def _closure_fixture(tmp_path: Path, mode: str = "partial") -> dict[str, Path]:
    general_closure = mode in {"complete", "blocked"}
    v3 = _v3_fixture(tmp_path, mode)
    partial_path = v3["seal_report"]
    pre_path = v3["dependency_snapshot"]
    assert isinstance(partial_path, Path) and isinstance(pre_path, Path)
    partial = json.loads(partial_path.read_bytes())
    pre = json.loads(pre_path.read_bytes())
    pre["dependency_hashes"] = partial["dependencies"]["current_hashes"]
    pre_bytes = _json_bytes(pre)
    pre_path.write_bytes(pre_bytes)
    partial["dependencies"]["pre_execution_snapshot_sha256"] = _sha(pre_bytes)
    if general_closure:
        for slot in partial["slots"]:
            run_record = slot["artifacts"]["run_record"]
            if run_record is not None:
                slot["artifacts"]["run_record"] = _closure_record_reference(
                    slot, "run.json", run_record["sha256"], run_record["bytes"],
                )
    partial_bytes = _json_bytes(partial)
    partial_path.write_bytes(partial_bytes)

    plan_path = v3["plan"]
    runtime_path = v3["runtime_config"]
    assert isinstance(plan_path, Path) and isinstance(runtime_path, Path)
    plan_bytes = plan_path.read_bytes()
    runtime_bytes = runtime_path.read_bytes()
    post_slots = []
    for slot in partial["slots"]:
        result = slot["artifacts"]["slot_result"]
        state = (
            "unstarted" if not slot["started"]
            else "started_completed" if slot["host_completed"]
            else "started_failed"
        )
        post_slot = {
            "slot_id": slot["slot_id"],
            "run_order": slot["run_order"],
            "state": state,
            "slot_result_sha256": result["sha256"] if result else None,
            "failure_ledger_sha256": None,
            "source_bundle_manifest_sha256": slot["source_bundle_manifest_sha256"],
        }
        if general_closure:
            run_record = slot["artifacts"]["run_record"]
            post_slot["run_record_sha256"] = run_record["sha256"] if run_record else None
            post_slot["score_record"] = (
                _closure_record_reference(slot, "score.json", "d" * 64, 23)
                if run_record else None
            )
        post_slots.append(post_slot)
    started_count = sum(slot["state"] != "unstarted" for slot in post_slots)
    failed_count = sum(slot["state"] == "started_failed" for slot in post_slots)
    unstarted_count = len(post_slots) - started_count
    score_record_count = sum(slot.get("score_record") is not None for slot in post_slots)
    post = {
        "schema_version": 1,
        "type": "post_execution_dependency_snapshot",
        "benchmark_id": partial["benchmark_id"],
        "source_commit": partial["source_commit"],
        "plan_state": "go",
        "execution_authorized": False,
        "plan_sha256": _sha(plan_bytes),
        "runtime_config_sha256": _sha(runtime_bytes),
        "immutable_plan_sha256": partial["immutable_plan_sha256"],
        "dependency_hashes": pre["dependency_hashes"],
        "dependency_changed_paths": [],
        "counts": {
            "planned_slots": 12,
            "started_slots": started_count,
            "failed_slots": failed_count,
            "unstarted_slots": unstarted_count,
            **({"score_records": score_record_count} if general_closure else {}),
        },
        "slots": post_slots,
    }
    post_path = tmp_path / "snapshot-after-closure.json"
    post_bytes = _json_bytes(post)
    post_path.write_bytes(post_bytes)

    ab_summary_path = v3["ab_summary"]
    assert isinstance(ab_summary_path, Path)
    ab_summary_bytes = ab_summary_path.read_bytes()
    closure = {
        "schema_version": 1,
        "type": "independent_final_closure_report" if general_closure else "independent_final_failure_report",
        "benchmark_id": partial["benchmark_id"],
        "execution_state": "completed" if mode == "complete" else "closed_incomplete",
        "plan_state": "go",
        "execution_authorized": True,
        "plan_sha256": _sha(plan_bytes),
        "immutable_plan_sha256": partial["immutable_plan_sha256"],
        "counts": {
            "planned_slots": 12,
            "started_slots": started_count,
            "failed_slots": failed_count,
            "unstarted_slots": unstarted_count,
            "host_completed_slots": partial["counts"]["host_completed_slots"],
            "evidence_valid_slots": partial["counts"]["evidence_valid_slots"],
            "eligible_pairs": partial["counts"]["eligible_pairs"],
            "planned_pairs": 6,
            "run_records": sum(slot["artifacts"]["run_record"] is not None for slot in partial["slots"]),
            "score_records": score_record_count,
        },
        "strict_postprocessing": (
            {
                "build": {
                    "exit_code": 0,
                    "created_run_records": score_record_count,
                    "created_score_records": score_record_count,
                },
                "validate": "passed" if mode == "complete" else "blocked",
                "score": "passed" if mode == "complete" else "blocked",
                "summarize": {
                    "exit_code": 0,
                    "ab_summary_sha256": _sha(ab_summary_bytes),
                },
            }
            if general_closure else {
                "build": {"exit_code": 0, "created_run_records": 0},
                "validate": "not_applicable; build created zero run records",
                "score": "not_applicable; build created zero run records",
                "summarize": "not_applicable; build created zero run records",
            }
        ),
        "sealing": {
            "pre_execution_snapshot_sha256": _sha(pre_bytes),
            "post_execution_snapshot_sha256": _sha(post_bytes),
            "post_execution_dependency_count": len(pre["dependency_hashes"]),
            "changed_dependencies": 0,
            "partial_workflow_report_sha256": _sha(partial_bytes),
            "partial_workflow_seal_status": "accepted",
            **({"ab_summary_sha256": _sha(ab_summary_bytes)} if general_closure else {}),
        },
        "artifact_hashes": {"plan": _sha(plan_bytes), "runtime_config": _sha(runtime_bytes)},
    }
    if not general_closure:
        closure["attempted_slot"] = {
            "slot_id": partial["slots"][0]["slot_id"],
            "run_order": 1,
            "run_status": "failed",
            "wrapper_exit_code": 1,
            "host_completed": False,
            "evidence_valid": False,
            "retry_count": 0,
        }
    closure_path = tmp_path / "final-report-closure.json"
    closure_path.write_bytes(_json_bytes(closure))
    result = {
        "closure_report": closure_path,
        "post_snapshot": post_path,
        "partial_report": partial_path,
        "pre_snapshot": pre_path,
        "plan": plan_path,
        "runtime_config": runtime_path,
    }
    if general_closure:
        result["ab_summary"] = ab_summary_path
    return result


def _refresh_closure_bindings(sources: dict[str, Path]) -> None:
    closure_path = sources["closure_report"]
    closure = json.loads(closure_path.read_bytes())
    closure["sealing"]["partial_workflow_report_sha256"] = _sha(
        sources["partial_report"].read_bytes(),
    )
    closure["sealing"]["post_execution_snapshot_sha256"] = _sha(
        sources["post_snapshot"].read_bytes(),
    )
    if "ab_summary" in sources:
        summary_hash = _sha(sources["ab_summary"].read_bytes())
        closure["sealing"]["ab_summary_sha256"] = summary_hash
        closure["strict_postprocessing"]["summarize"]["ab_summary_sha256"] = summary_hash
    closure_path.write_bytes(_json_bytes(closure))


def _postprocessed_incomplete_fixture(tmp_path: Path) -> dict[str, Path]:
    sources = _closure_fixture(tmp_path, "complete")
    partial = json.loads(sources["partial_report"].read_bytes())
    post = json.loads(sources["post_snapshot"].read_bytes())
    closure = json.loads(sources["closure_report"].read_bytes())
    ab_summary = json.loads(sources["ab_summary"].read_bytes())

    partial["ab_eval"] = {"status": "not_generated", "pairs": []}
    for index, slot in enumerate(partial["slots"]):
        if index == 0:
            slot["evidence_valid"] = False
            continue
        slot["started"] = False
        slot["host_completed"] = False
        slot["evidence_valid"] = False
        slot["status"] = "not_started"
        slot["artifacts"]["slot_result"] = None
        slot["artifacts"]["run_record"] = None
        slot["sealed_artifact_check"] = {
            "status": "not_applicable",
            "references_checked": 0,
            "mismatches": [],
        }
    for pair in partial["pairs"]:
        pair["ab_eval_status"] = "pair_incomplete_missing_run_record"
        pair["eligible"] = False
    partial["counts"].update({
        "started_slots": 1,
        "host_completed_slots": 1,
        "evidence_valid_slots": 0,
        "eligible_pairs": 0,
    })
    partial["sealed_artifacts"].update({
        "references_checked": 1,
        "unavailable_slot_results": 11,
    })

    for index, slot in enumerate(post["slots"]):
        if index == 0:
            continue
        slot["state"] = "unstarted"
        slot["slot_result_sha256"] = None
        slot["run_record_sha256"] = None
        slot["score_record"] = None
    post["counts"].update({
        "started_slots": 1,
        "failed_slots": 0,
        "unstarted_slots": 11,
        "score_records": 1,
    })

    closure["execution_state"] = "closed_incomplete"
    closure["counts"].update({
        "started_slots": 1,
        "failed_slots": 0,
        "unstarted_slots": 11,
        "host_completed_slots": 1,
        "evidence_valid_slots": 0,
        "eligible_pairs": 0,
        "run_records": 1,
        "score_records": 1,
    })
    closure["strict_postprocessing"]["build"]["created_run_records"] = 1
    closure["strict_postprocessing"]["build"]["created_score_records"] = 1

    ab_summary["run_count"] = 1
    ab_summary["pending_annotation_case_count"] = 1
    ab_summary["paired_cases"] = [
        {
            "case_id": "case0002",
            "replicate": 1,
            "status": "pair_incomplete",
            "conditions_present": ["same_host_direct"],
            "attempts": {"same_host_direct": 1, "current_skill": 0},
        },
        {
            "case_id": "case0003",
            "replicate": None,
            "status": "pair_incomplete",
            "conditions_present": [],
            "attempts": {"same_host_direct": 0, "current_skill": 0},
        },
        {
            "case_id": "case0006",
            "replicate": None,
            "status": "pair_incomplete",
            "conditions_present": [],
            "attempts": {"same_host_direct": 0, "current_skill": 0},
        },
    ]
    ab_summary["missing_expected_runs"] = [
        {"case_id": "case0002", "condition": "current_skill", "status": "no_run_record"},
        {"case_id": "case0003", "condition": "current_skill", "status": "no_run_record"},
        {"case_id": "case0003", "condition": "same_host_direct", "status": "no_run_record"},
        {"case_id": "case0006", "condition": "current_skill", "status": "no_run_record"},
        {"case_id": "case0006", "condition": "same_host_direct", "status": "no_run_record"},
    ]

    sources["partial_report"].write_bytes(_json_bytes(partial))
    sources["post_snapshot"].write_bytes(_json_bytes(post))
    sources["closure_report"].write_bytes(_json_bytes(closure))
    sources["ab_summary"].write_bytes(_json_bytes(ab_summary))
    _refresh_closure_bindings(sources)
    return sources


def _build_closure(
    tmp_path: Path,
    sources: dict[str, Path],
    batch_id: str = "custom-batch-2026",
) -> tuple[Path, Path, Path]:
    output = tmp_path / "closure-export"
    outputs = (
        output / "custom-batch-summary.json",
        output / "custom-batch-evidence.json",
        output / "custom-batch-check.json",
    )
    exporter.build_closure_export(
        sources["closure_report"],
        sources["post_snapshot"],
        sources["partial_report"],
        sources["pre_snapshot"],
        sources["plan"],
        sources["runtime_config"],
        *outputs,
        ab_summary_path=sources.get("ab_summary"),
        ab_summary_not_generated="ab_summary" not in sources,
        expected_closure_sha256=_sha(sources["closure_report"].read_bytes()),
        expected_post_snapshot_sha256=_sha(sources["post_snapshot"].read_bytes()),
        batch_id=batch_id,
    )
    return outputs


@pytest.mark.parametrize(
    ("mode", "status", "run_count", "pending_count", "strict_status"),
    [
        ("complete", "completed", 12, 3, "validated"),
        ("blocked", "incomplete", 1, 1, "blocked_label_reference_present"),
    ],
)
def test_closure_export_projects_completed_and_nonzero_incomplete_closures(
    tmp_path: Path,
    mode: str,
    status: str,
    run_count: int,
    pending_count: int,
    strict_status: str,
) -> None:
    sources = _closure_fixture(tmp_path, mode)
    summary_path, evidence_path, check_path = _build_closure(tmp_path, sources)
    summary_bytes = summary_path.read_bytes()
    summary = json.loads(summary_bytes)
    evidence = json.loads(evidence_path.read_bytes())
    check = json.loads(check_path.read_bytes())

    assert check["status"] == "passed"
    assert summary["execution_state"] == ("completed" if mode == "complete" else "closed_incomplete")
    assert summary["status"] == status
    assert summary["workflow"]["host_completed_slots"] == run_count
    assert summary["workflow"]["not_started_slots"] == 12 - run_count
    assert summary["strict_ab_eval"]["run_records_created"] == run_count
    assert summary["strict_ab_eval"]["score_files_created"] == run_count
    assert summary["strict_ab_eval"]["status"] == strict_status
    assert summary["human_adjudication"]["status"] == "pending"
    assert summary["human_adjudication"]["labels_read"] is False
    assert summary["human_adjudication"]["pending_case_count"] == pending_count
    assert summary["human_adjudication"]["precision"] is None
    assert summary["human_adjudication"]["recall"] is None
    assert summary["human_adjudication"]["clean_case_false_alarms"] is None
    assert summary["human_adjudication"]["wrong_resolution_count"] is None
    assert summary["model_cost"] == {"status": "unknown", "amount": None, "currency": None}
    assert evidence["source_names"]["ab_summary"] == sources["ab_summary"].name
    assert b"DO_NOT_EXPORT_V3" not in summary_bytes + evidence_path.read_bytes()
    if mode == "complete":
        assert summary["workflow"]["eligible_pairs"] == 6
        assert summary["workflow"]["evidence_valid_slots"] == 12
        assert summary["strict_ab_eval"]["validate_status"] == "passed"
        assert summary["strict_ab_eval"]["score_status"] == "passed"
        partial = json.loads(sources["partial_report"].read_bytes())
        post = json.loads(sources["post_snapshot"].read_bytes())
        run_refs = [slot["artifacts"]["run_record"] for slot in partial["slots"]]
        score_refs = [slot["score_record"] for slot in post["slots"]]
        assert len({ref["source"] for ref in run_refs}) == 12
        assert len({ref["sha256"] for ref in run_refs}) == 1
        assert len({ref["source"] for ref in score_refs}) == 12
        assert len({ref["sha256"] for ref in score_refs}) == 1
    else:
        assert summary["workflow"]["eligible_pairs"] == 0
        assert summary["workflow"]["evidence_valid_slots"] == 1
        assert summary["strict_ab_eval"]["build_status"] == "blocked"
        assert summary["strict_ab_eval"]["score_status"] == "blocked"


def test_legacy_v3_closure_projection_retains_committed_schema(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path)
    summary_path, evidence_path, check_path = _build_closure(
        tmp_path, sources, batch_id=exporter.V3_EXPORT_ID,
    )
    summary_bytes = summary_path.read_bytes()
    summary = json.loads(summary_bytes)
    check = json.loads(check_path.read_bytes())

    assert summary["exporter_version"] == "2.1.0"
    assert "score_files_created" not in summary["strict_ab_eval"]
    assert summary["strict_ab_eval"]["run_records_created"] == 0
    assert summary["strict_ab_eval"]["validate_status"] == "not_applicable"
    assert json.loads(evidence_path.read_bytes())["exporter"]["version"] == "2.1.0"
    assert check["status"] == "passed"


def test_closure_export_rejects_completed_state_without_run_records(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path, "complete")
    partial = json.loads(sources["partial_report"].read_bytes())
    for slot in partial["slots"]:
        slot["artifacts"]["run_record"] = None
    partial["ab_eval"] = {"status": "not_generated", "pairs": []}
    for pair in partial["pairs"]:
        pair["ab_eval_status"] = "pair_incomplete_missing_run_record"
        pair["eligible"] = False
    partial["counts"]["eligible_pairs"] = 0
    sources["partial_report"].write_bytes(_json_bytes(partial))

    post = json.loads(sources["post_snapshot"].read_bytes())
    for slot in post["slots"]:
        slot["run_record_sha256"] = None
        slot["score_record"] = None
    post["counts"]["score_records"] = 0
    sources["post_snapshot"].write_bytes(_json_bytes(post))

    closure = json.loads(sources["closure_report"].read_bytes())
    closure["counts"]["eligible_pairs"] = 0
    closure["counts"]["run_records"] = 0
    closure["counts"]["score_records"] = 0
    closure["strict_postprocessing"]["build"]["created_run_records"] = 0
    closure["strict_postprocessing"]["build"]["created_score_records"] = 0
    sources["closure_report"].write_bytes(_json_bytes(closure))
    sources.pop("ab_summary")
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError, match="completed closure does not contain"):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


def test_closure_export_rejects_completed_summary_without_all_planned_pairs(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path, "complete")
    ab_summary = json.loads(sources["ab_summary"].read_bytes())
    partial = json.loads(sources["partial_report"].read_bytes())
    closure = json.loads(sources["closure_report"].read_bytes())
    ab_summary["paired_cases"] = []
    partial["ab_eval"] = {"status": "not_generated", "pairs": []}
    for pair in partial["pairs"]:
        pair["ab_eval_status"] = "pair_incomplete_missing_run_record"
        pair["eligible"] = False
    partial["counts"]["eligible_pairs"] = 0
    closure["counts"]["eligible_pairs"] = 0
    sources["ab_summary"].write_bytes(_json_bytes(ab_summary))
    sources["partial_report"].write_bytes(_json_bytes(partial))
    sources["closure_report"].write_bytes(_json_bytes(closure))
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError, match="strict pair identities disagree with observed sealed runs"):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


def test_incomplete_closure_accepts_observed_run_aggregates_without_pair_eligibility(
    tmp_path: Path,
) -> None:
    sources = _postprocessed_incomplete_fixture(tmp_path)
    summary_path, evidence_path, check_path = _build_closure(tmp_path, sources)
    summary = json.loads(summary_path.read_bytes())
    evidence = json.loads(evidence_path.read_bytes())
    check = json.loads(check_path.read_bytes())

    assert check["status"] == "passed"
    assert summary["execution_state"] == "closed_incomplete"
    assert summary["status"] == "incomplete"
    assert summary["workflow"]["host_completed_slots"] == 1
    assert summary["workflow"]["evidence_valid_slots"] == 0
    assert summary["workflow"]["eligible_pairs"] == 0
    assert len(summary["workflow"]["pairs"]) == 6
    assert {pair["status"] for pair in summary["workflow"]["pairs"]} == {
        "pair_incomplete_missing_run_record",
    }
    assert summary["strict_ab_eval"]["run_records_created"] == 1
    assert summary["strict_ab_eval"]["score_files_created"] == 1
    assert summary["strict_ab_eval"]["status"] == "validated"
    assert summary["strict_ab_eval"]["validate_status"] == "passed"
    assert summary["strict_ab_eval"]["score_status"] == "passed"
    assert evidence["source_status"]["ab_summary"] == "provided"


def test_completed_closure_rejects_incomplete_strict_pair_statuses(
    tmp_path: Path,
) -> None:
    sources = _closure_fixture(tmp_path, "complete")
    ab_summary = json.loads(sources["ab_summary"].read_bytes())
    partial = json.loads(sources["partial_report"].read_bytes())
    ab_summary["paired_cases"] = [
        {"case_id": case_id, "replicate": replicate, "status": "pair_incomplete"}
        for case_id in ("case0002", "case0003", "case0006")
        for replicate in (1, 2)
    ]
    partial["ab_eval"] = {
        "status": "validated",
        "pairs": [
            {"case_id": pair["case_id"], "replicate": pair["replicate"], "status": pair["status"]}
            for pair in ab_summary["paired_cases"]
        ],
    }
    sources["ab_summary"].write_bytes(_json_bytes(ab_summary))
    sources["partial_report"].write_bytes(_json_bytes(partial))
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError, match="pair status disagrees with strict summary"):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


def test_case_level_summary_placeholder_cannot_cover_an_observed_run(
    tmp_path: Path,
) -> None:
    sources = _postprocessed_incomplete_fixture(tmp_path)
    ab_summary = json.loads(sources["ab_summary"].read_bytes())
    ab_summary["paired_cases"][0]["replicate"] = None
    sources["ab_summary"].write_bytes(_json_bytes(ab_summary))
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError, match="case-level pair placeholder has an observed run"):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


def test_postprocessed_summary_pair_replicate_must_match_observed_run(
    tmp_path: Path,
) -> None:
    sources = _postprocessed_incomplete_fixture(tmp_path)
    ab_summary = json.loads(sources["ab_summary"].read_bytes())
    ab_summary["paired_cases"][0]["replicate"] = 2
    sources["ab_summary"].write_bytes(_json_bytes(ab_summary))
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError, match="strict pair identities disagree with observed sealed runs"):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


def test_postprocessed_summary_missing_runs_must_match_observed_conditions(
    tmp_path: Path,
) -> None:
    sources = _postprocessed_incomplete_fixture(tmp_path)
    ab_summary = json.loads(sources["ab_summary"].read_bytes())
    ab_summary["missing_expected_runs"][-1] = {
        "case_id": "case0002",
        "condition": "same_host_direct",
        "status": "no_run_record",
    }
    sources["ab_summary"].write_bytes(_json_bytes(ab_summary))
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError, match="missing expected runs disagree with sealed run identities"):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


def test_closure_export_rejects_completed_closure_with_ineligible_pair(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path, "complete")
    ab_summary = json.loads(sources["ab_summary"].read_bytes())
    partial = json.loads(sources["partial_report"].read_bytes())
    closure = json.loads(sources["closure_report"].read_bytes())
    pair_status = "pair_incomplete_missing_run_record"
    ab_summary["paired_cases"][0]["status"] = pair_status
    partial["ab_eval"]["pairs"][0]["status"] = pair_status
    partial["pairs"][0]["ab_eval_status"] = pair_status
    partial["pairs"][0]["eligible"] = False
    partial["counts"]["eligible_pairs"] = 5
    closure["counts"]["eligible_pairs"] = 5
    sources["ab_summary"].write_bytes(_json_bytes(ab_summary))
    sources["partial_report"].write_bytes(_json_bytes(partial))
    sources["closure_report"].write_bytes(_json_bytes(closure))
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError, match="six eligible planned pair results"):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


@pytest.mark.parametrize(
    ("artifact_kind", "tamper"),
    [
        ("run", "wrong_identity"),
        ("run", "duplicate_source"),
        ("run", "empty_source"),
        ("run", "zero_bytes"),
        ("score", "wrong_identity"),
        ("score", "duplicate_source"),
        ("score", "empty_source"),
        ("score", "zero_bytes"),
    ],
)
def test_closure_export_rejects_unbound_or_reused_record_references(
    tmp_path: Path,
    artifact_kind: str,
    tamper: str,
) -> None:
    sources = _closure_fixture(tmp_path, "complete")
    partial = json.loads(sources["partial_report"].read_bytes())
    post = json.loads(sources["post_snapshot"].read_bytes())
    if artifact_kind == "run":
        refs = [slot["artifacts"]["run_record"] for slot in partial["slots"]]
    else:
        refs = [slot["score_record"] for slot in post["slots"]]
    if tamper == "wrong_identity":
        refs[1]["identity"]["case_id"] = refs[0]["identity"]["case_id"]
    elif tamper == "duplicate_source":
        refs[1]["source"] = refs[0]["source"]
    elif tamper == "empty_source":
        refs[0]["source"] = ""
    else:
        refs[0]["bytes"] = 0
    if artifact_kind == "run":
        sources["partial_report"].write_bytes(_json_bytes(partial))
    else:
        sources["post_snapshot"].write_bytes(_json_bytes(post))
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


@pytest.mark.parametrize(
    ("field", "value", "condition"),
    [
        ("run_count", True, None),
        ("adjudicated_case_count", False, None),
        ("pending_annotation_case_count", True, None),
        ("human_precision_case_count", False, "same_host_direct"),
        ("known_defect_recall_case_count", False, "current_skill"),
    ],
)
def test_closure_export_rejects_boolean_ab_summary_counts(
    tmp_path: Path,
    field: str,
    value: bool,
    condition: str | None,
) -> None:
    sources = _closure_fixture(tmp_path, "blocked")
    ab_summary = json.loads(sources["ab_summary"].read_bytes())
    if condition is None:
        ab_summary[field] = value
    else:
        ab_summary["by_condition"][condition][field] = value
    sources["ab_summary"].write_bytes(_json_bytes(ab_summary))
    _refresh_closure_bindings(sources)

    with pytest.raises(exporter.ExportError, match="count is invalid"):
        _build_closure(tmp_path, sources)
    assert not (tmp_path / "closure-export").exists()


def test_closure_export_rejects_boolean_score_counts(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path, "blocked")
    closure = json.loads(sources["closure_report"].read_bytes())
    closure["counts"]["score_records"] = True
    sources["closure_report"].write_bytes(_json_bytes(closure))

    with pytest.raises(exporter.ExportError, match="closure score_records count"):
        _build_closure(tmp_path, sources)


@pytest.mark.parametrize(
    ("mode", "workflow_status", "run_count", "eligible_pairs", "strict_status"),
    [
        ("complete", "completed", 12, 6, "validated"),
        ("partial", "failed", 0, 0, "not_generated"),
        ("blocked", "incomplete", 1, 0, "blocked_label_reference_present"),
        ("empty", "incomplete", 0, 0, "not_generated"),
    ],
)
def test_v3_export_projects_workflow_strict_eval_and_pending_labels(
    tmp_path: Path,
    mode: str,
    workflow_status: str,
    run_count: int,
    eligible_pairs: int,
    strict_status: str,
) -> None:
    sources = _v3_fixture(tmp_path, mode)
    summary_path, evidence_path, check_path = _build_v3(tmp_path, sources)
    summary = json.loads(summary_path.read_bytes())
    evidence = json.loads(evidence_path.read_bytes())
    check = json.loads(check_path.read_bytes())

    assert check["status"] == "passed"
    assert check["export_id"] == exporter.V3_EXPORT_ID
    assert summary["status"] == workflow_status
    assert summary["workflow"]["eligible_pairs"] == eligible_pairs
    assert summary["strict_ab_eval"]["run_records_created"] == run_count
    assert summary["strict_ab_eval"]["status"] == strict_status
    assert summary["human_adjudication"]["status"] == "pending"
    assert summary["human_adjudication"]["labels_read"] is False
    assert summary["human_adjudication"]["precision"] is None
    assert evidence["source_names"] == exporter.V3_SOURCE_NAMES
    assert b"DO_NOT_EXPORT_V3" not in summary_path.read_bytes() + evidence_path.read_bytes()
    if mode == "partial":
        assert summary["workflow"]["failed_slots"] == 1
        assert summary["workflow"]["not_started_slots"] == 11
        assert summary["integrity"]["artifact_reference_check"] == {
            "reported_status": "no_mismatches",
            "references_checked": 98,
            "unavailable_slot_results": 11,
        }
    if mode == "empty":
        assert summary["workflow"]["not_started_slots"] == 12
        assert summary["integrity"]["seal_completeness"] == "incomplete"
    if mode == "complete":
        assert summary["strict_ab_eval"]["validate_status"] == "passed"
        assert summary["human_adjudication"]["pending_case_count"] == 3
    if mode == "blocked":
        assert summary["strict_ab_eval"]["build_status"] == "blocked"
        assert summary["strict_ab_eval"]["score_status"] == "blocked"
        assert summary["workflow"]["host_completed_slots"] == 1


@pytest.mark.parametrize("tamper", ["counts", "pair_eligibility", "source_bundle", "human_quality"])
def test_v3_export_rejects_forged_counts_and_pair_eligibility(tmp_path: Path, tamper: str) -> None:
    sources = _v3_fixture(tmp_path, "partial")
    report_path = sources["seal_report"]
    assert isinstance(report_path, Path)
    report = json.loads(report_path.read_bytes())
    if tamper == "counts":
        report["counts"]["started_slots"] = 2
    elif tamper == "pair_eligibility":
        report["pairs"][0]["eligible"] = True
    elif tamper == "source_bundle":
        report["slots"][0]["preseal_manifest_matches_snapshot"] = False
    else:
        report["human_quality"]["precision"] = 0.5
    report_path.write_bytes(_json_bytes(report))

    with pytest.raises(exporter.ExportError):
        _build_v3(tmp_path, sources)


def test_v3_check_rejects_summary_and_source_tampering(tmp_path: Path) -> None:
    sources = _v3_fixture(tmp_path, "partial")
    summary_path, evidence_path, _check_path = _build_v3(tmp_path, sources)
    source_bytes = {name: value.read_bytes() for name, value in sources.items() if isinstance(value, Path)}
    modified_summary = json.loads(summary_path.read_bytes())
    modified_summary["workflow"]["not_started_slots"] = 10
    with pytest.raises(exporter.ExportError, match="summary bytes differ"):
        exporter.check_export(
            _json_bytes(modified_summary), evidence_path.read_bytes(),
            seal_report_bytes=source_bytes["seal_report"],
            ab_summary_bytes=source_bytes["ab_summary"],
            dependency_snapshot_bytes=source_bytes["dependency_snapshot"],
            plan_bytes=source_bytes["plan"],
            runtime_config_bytes=source_bytes["runtime_config"],
            export_id=exporter.V3_EXPORT_ID,
        )
    report = json.loads(source_bytes["seal_report"])
    report["dependencies"]["changed_paths"] = ["plan"]
    with pytest.raises(exporter.ExportError, match="changed paths"):
        exporter.check_export(
            summary_path.read_bytes(), evidence_path.read_bytes(),
            seal_report_bytes=_json_bytes(report),
            ab_summary_bytes=source_bytes["ab_summary"],
            dependency_snapshot_bytes=source_bytes["dependency_snapshot"],
            plan_bytes=source_bytes["plan"],
            runtime_config_bytes=source_bytes["runtime_config"],
            export_id=exporter.V3_EXPORT_ID,
        )


def test_closure_export_marks_missing_summary_and_checks_hash_pinned_projection(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path)
    summary_path, evidence_path, check_path = _build_closure(tmp_path, sources)
    summary_bytes = summary_path.read_bytes()
    evidence_bytes = evidence_path.read_bytes()
    summary = json.loads(summary_bytes)
    evidence = json.loads(evidence_bytes)
    check = json.loads(check_path.read_bytes())

    assert check["status"] == "passed"
    assert check["export_id"] == "custom-batch-2026"
    assert summary["workflow"]["launched_slots"] == 1
    assert summary["workflow"]["failed_slots"] == 1
    assert summary["workflow"]["not_started_slots"] == 11
    assert summary["strict_ab_eval"] == {
        "run_records_created": 0,
        "score_files_created": 0,
        "status": "not_generated",
        "build_status": "not_generated",
        "validate_status": "not_applicable",
        "score_status": "not_applicable",
    }
    assert summary["source_ab_summary"] == {
        "status": "not_generated",
        "run_count": 0,
        "adjudicated_case_count": 0,
        "pending_annotation_case_count": 0,
    }
    assert evidence["source_status"]["ab_summary"] == "not_generated"
    assert evidence["source_names"]["closure_report"] == sources["closure_report"].name
    assert evidence["source_names"]["ab_summary"] is None
    assert b"DO_NOT_EXPORT" not in summary_bytes + evidence_bytes
    assert str(tmp_path).encode() not in summary_bytes + evidence_bytes

    source_names = {
        "closure_report": sources["closure_report"].name,
        "post_execution_snapshot": sources["post_snapshot"].name,
        "partial_workflow_report": sources["partial_report"].name,
        "pre_execution_snapshot": sources["pre_snapshot"].name,
        "plan": sources["plan"].name,
        "runtime_config": sources["runtime_config"].name,
        "ab_summary": None,
    }
    result = exporter.check_closure_export(
        summary_bytes,
        evidence_bytes,
        closure_report_bytes=sources["closure_report"].read_bytes(),
        post_execution_snapshot_bytes=sources["post_snapshot"].read_bytes(),
        partial_workflow_report_bytes=sources["partial_report"].read_bytes(),
        pre_execution_snapshot_bytes=sources["pre_snapshot"].read_bytes(),
        plan_bytes=sources["plan"].read_bytes(),
        runtime_config_bytes=sources["runtime_config"].read_bytes(),
        ab_summary_not_generated=True,
        expected_closure_sha256=_sha(sources["closure_report"].read_bytes()),
        expected_post_snapshot_sha256=_sha(sources["post_snapshot"].read_bytes()),
        batch_id="custom-batch-2026",
        source_names=source_names,
    )
    assert result["status"] == "passed"


def test_closure_export_rejects_unknown_slot_status_without_publishing(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path)
    partial = json.loads(sources["partial_report"].read_bytes())
    partial["slots"][1]["status"] = "mystery_state"
    partial_bytes = _json_bytes(partial)
    sources["partial_report"].write_bytes(partial_bytes)
    closure = json.loads(sources["closure_report"].read_bytes())
    closure["sealing"]["partial_workflow_report_sha256"] = _sha(partial_bytes)
    sources["closure_report"].write_bytes(_json_bytes(closure))

    output = tmp_path / "rejected-closure"
    with pytest.raises(exporter.ExportError, match="slot status is unsupported"):
        exporter.build_closure_export(
            sources["closure_report"], sources["post_snapshot"], sources["partial_report"],
            sources["pre_snapshot"], sources["plan"], sources["runtime_config"],
            output / "summary.json", output / "evidence.json", output / "check.json",
            ab_summary_not_generated=True,
            expected_closure_sha256=_sha(sources["closure_report"].read_bytes()),
            expected_post_snapshot_sha256=_sha(sources["post_snapshot"].read_bytes()),
            batch_id="custom-batch-2026",
        )
    assert not output.exists()


def test_closure_export_requires_real_summary_when_run_record_is_sealed(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path)
    partial = json.loads(sources["partial_report"].read_bytes())
    partial["slots"][0]["artifacts"]["run_record"] = {
        "path": "artifacts/run.json", "sha256": "d" * 64, "bytes": 12,
    }
    partial_bytes = _json_bytes(partial)
    sources["partial_report"].write_bytes(partial_bytes)
    closure = json.loads(sources["closure_report"].read_bytes())
    closure["sealing"]["partial_workflow_report_sha256"] = _sha(partial_bytes)
    sources["closure_report"].write_bytes(_json_bytes(closure))

    output = tmp_path / "summary-required"
    with pytest.raises(exporter.ExportError, match="summary is required when run records exist"):
        exporter.build_closure_export(
            sources["closure_report"], sources["post_snapshot"], sources["partial_report"],
            sources["pre_snapshot"], sources["plan"], sources["runtime_config"],
            output / "summary.json", output / "evidence.json", output / "check.json",
            ab_summary_not_generated=True,
            expected_closure_sha256=_sha(sources["closure_report"].read_bytes()),
            expected_post_snapshot_sha256=_sha(sources["post_snapshot"].read_bytes()),
            batch_id="custom-batch-2026",
        )
    assert not output.exists()


def test_closure_export_rejects_source_tampering_before_overwriting_outputs(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path)
    summary_output = tmp_path / "existing-summary.json"
    summary_output.write_bytes(b"preserve existing export\n")
    tampered_post = sources["post_snapshot"].read_bytes() + b" "
    sources["post_snapshot"].write_bytes(tampered_post)

    with pytest.raises(exporter.ExportError, match="pinned post-execution snapshot byte hash"):
        exporter.build_closure_export(
            sources["closure_report"], sources["post_snapshot"], sources["partial_report"],
            sources["pre_snapshot"], sources["plan"], sources["runtime_config"],
            summary_output, tmp_path / "evidence.json", tmp_path / "check.json",
            ab_summary_not_generated=True,
            expected_closure_sha256=_sha(sources["closure_report"].read_bytes()),
            expected_post_snapshot_sha256=_sha(tampered_post[:-1]),
            batch_id="custom-batch-2026",
        )
    assert summary_output.read_bytes() == b"preserve existing export\n"
    assert not (tmp_path / "evidence.json").exists()


def test_closure_export_requires_expected_hash_pins(tmp_path: Path) -> None:
    sources = _closure_fixture(tmp_path)
    output = tmp_path / "un-pinned-closure"
    with pytest.raises(exporter.ExportError, match="expected closure SHA-256 pin is required"):
        exporter.build_closure_export(
            sources["closure_report"], sources["post_snapshot"], sources["partial_report"],
            sources["pre_snapshot"], sources["plan"], sources["runtime_config"],
            output / "summary.json", output / "evidence.json", output / "check.json",
            ab_summary_not_generated=True,
            expected_post_snapshot_sha256=_sha(sources["post_snapshot"].read_bytes()),
            batch_id="custom-batch-2026",
        )
    assert not output.exists()


def test_public_v4_closure_export_is_hash_pinned_and_safe() -> None:
    results = Path(__file__).parents[1] / "eval" / "results"
    paths = {
        "summary": results / "2026-10-12-ab-workflow-validation-v4-summary.json",
        "evidence": results / "2026-10-12-ab-workflow-validation-v4-evidence.json",
        "check": results / "2026-10-12-ab-workflow-validation-v4-check.json",
    }
    expected_hashes = {
        "summary": "a01af725b67b7116c8731954c98536d0a169037c86bad103ef6a66e7ced69732",
        "evidence": "c27f808b9c2491efe328811223dade78053ea3e9f746eadaa41afdda1612fe14",
        "check": "4ebd68195a205d3c0de478597fa7387efdf6c76d6fe7d61f44fb0426f5032112",
    }
    artifacts = {name: path.read_bytes() for name, path in paths.items()}
    assert {name: _sha(payload) for name, payload in artifacts.items()} == expected_hashes

    summary = json.loads(artifacts["summary"])
    evidence = json.loads(artifacts["evidence"])
    check = json.loads(artifacts["check"])
    export_id = "2026-10-12-ab-workflow-validation-v4"
    assert summary["export_id"] == evidence["export_id"] == check["export_id"] == export_id
    assert summary["schema_version"] == evidence["schema_version"] == check["schema_version"] == 1
    assert summary["exporter_version"] == evidence["exporter"]["version"] == "2.2.0"
    assert summary["execution_state"] == "closed_incomplete"
    assert summary["status"] == summary["workflow"]["status"] == "incomplete"
    assert summary["workflow"]["planned_slots"] == 12
    assert summary["workflow"]["host_completed_slots"] == 1
    assert summary["workflow"]["evidence_valid_slots"] == 0
    assert summary["workflow"]["eligible_pairs"] == 0
    assert summary["workflow"]["not_started_slots"] == 11
    assert summary["workflow"]["pair_status_counts"] == {
        "pair_incomplete": 1,
        "pair_incomplete_missing_run_record": 5,
    }
    assert summary["strict_ab_eval"]["status"] == "validated"
    assert summary["strict_ab_eval"]["validate_status"] == "passed"
    assert summary["strict_ab_eval"]["score_status"] == "passed"
    assert summary["human_adjudication"]["status"] == "pending"
    assert summary["human_adjudication"]["labels"] == []
    assert summary["human_adjudication"]["labels_read"] is False
    assert summary["human_adjudication"]["precision"] is None
    assert summary["human_adjudication"]["recall"] is None
    assert summary["model_cost"] == {"status": "unknown", "amount": None, "currency": None}

    assert evidence["source_names"] == {
        "ab_summary": "ab-summary-postprocess-v4.json",
        "closure_report": "closure-final-v4.json",
        "partial_workflow_report": "workflow-report-postprocessed-v4.json",
        "plan": "batch-plan-v4.json",
        "post_execution_snapshot": "dependencies-after-v4-final.json",
        "pre_execution_snapshot": "dependencies-before-v4-v2.json",
        "runtime_config": "runtime-config-v4.json",
    }
    assert evidence["source_hashes"] == {
        "ab_summary_sha256": "c46ab67fb9f5090ab58d979fc998d444f9a53e6c567010fd889e4528b16f0a94",
        "closure_report_sha256": "e14c0564bbec0b6abe5c913e410dc46063c2b24a00eac0d52d5bb8d163ee27c1",
        "partial_workflow_report_sha256": "bd4841ed587645934dc3f340a4c8cbb200710052354d621fbff059dc1c8a0538",
        "plan_sha256": "9ab5d465fa058c4984e9cac4ff8cb28c70e54bc8a256a4fed7d469fd6d7116b3",
        "post_execution_snapshot_sha256": "b5f33bdc5fb7ee59075a589b396dd45356464037355e28b82b4a1e2288d46aad",
        "pre_execution_snapshot_sha256": "7b7e8ec04c88147c1e1ecbec17cc5de2b0caf3a1f2083fb3e872a3d25c7f8729",
        "runtime_config_sha256": "6488d53483e83ec9b9ce7b6688063c3f66d3e3e7730ca1f7ba028270468b8f22",
    }
    assert check["status"] == "passed"
    assert all(value == "verified" for value in check["checks"].values())
    assert check["source_hashes"] == evidence["source_hashes"]
    assert evidence["summary_binding"]["sha256"] == expected_hashes["summary"]
    assert evidence["summary_binding"]["projection_sha256"] == expected_hashes["summary"]
    assert check["summary_sha256"] == expected_hashes["summary"]
    assert check["evidence_sha256"] == expected_hashes["evidence"]
    assert all(
        marker not in payload.lower()
        for payload in artifacts.values()
        for marker in (b"raw_output", b"prediction_body", b"api_key", b"credential", b"/root/project/")
    )

    v3_paths = {
        "summary": results / "2026-10-12-ab-workflow-validation-v3-summary.json",
        "evidence": results / "2026-10-12-ab-workflow-validation-v3-evidence.json",
        "check": results / "2026-10-12-ab-workflow-validation-v3-check.json",
    }
    v3_hashes = {
        "summary": "24aef235909d8c554b508fbbb20519742933789477cd5073357bc21608f01e5a",
        "evidence": "05a706ff69abeb76c6c6b417d89a1fdefdc0bc538454f5fb49dbd50f226d293f",
        "check": "4a424c4e79197cbc2efeebe9240863054e81a048085f2627d0de727957a798eb",
    }
    assert {name: _sha(path.read_bytes()) for name, path in v3_paths.items()} == v3_hashes
