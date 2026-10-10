from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from eval.export_ab_pilot import (
    EXPECTED_SOURCE_HASHES,
    ExportError,
    SOURCE_DIGEST_FILES,
    SOURCE_DIGEST_SCOPES,
    build_export,
    build_historical_export,
    check_export,
    prepare_blind_review_bundle,
    validate_export,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture() -> tuple[dict, dict]:
    runs: list[dict] = []
    for case_number in range(1, 12):
        case_id = f"case{case_number:04d}"
        conditions = ("same_host_direct", "current_skill")
        for condition in conditions:
            attempts = (1, 2) if condition == "same_host_direct" and case_number == 1 else (1,)
            for attempt in attempts:
                completed = not (condition == "same_host_direct" and case_number == 1 and attempt == 1)
                raw_digest = _sha(f"{case_id}:{condition}:{attempt}")
                runs.append({
                    "case_id": case_id,
                    "condition": condition,
                    "replicate": 1,
                    "attempt": attempt,
                    "run_status": "completed" if completed else "incomplete",
                    "duration_seconds": 100.0 + len(runs) if completed else None,
                    "reported_finding_count": 1 if case_id in {"case0001", "case0003", "case0004", "case0008", "case0009", "case0011"} else 0,
                    "artifacts": {
                        "raw_host_output_sha256": raw_digest,
                        "raw_host_output_included": False,
                        "run_record_sha256": _sha(f"record:{case_id}:{condition}:{attempt}"),
                        "original_score_sha256": _sha(f"score:{case_id}:{condition}:{attempt}"),
                        "original_score_status": "preserved_only_not_replayed",
                    },
                    "review_manifest_digest_audit": {
                        "historical_status": "historical_input_binding_unknown",
                        "current_audit_digest": _sha(f"manifest:{case_id}:{condition}:{attempt}"),
                        "scope": "current_copy_bytes_observed_at_export",
                    },
                    "input": {"repository_id": "example/repo"},
                })

    predictions = []
    prediction_refs = [
        ("p0001", "case0001", "current_skill", 1, "supported"),
        ("p0002", "case0001", "same_host_direct", 2, "supported"),
        ("p0003", "case0001", "same_host_direct", 2, "supported"),
        ("p0004", "case0003", "current_skill", 1, "supported"),
        ("p0005", "case0003", "same_host_direct", 1, "supported"),
        ("p0006", "case0004", "same_host_direct", 1, "unresolved"),
        ("p0007", "case0008", "current_skill", 1, "unresolved"),
        ("p0008", "case0009", "current_skill", 1, "supported"),
        ("p0009", "case0009", "same_host_direct", 1, "supported"),
        ("p0010", "case0011", "current_skill", 1, "unresolved"),
    ]
    for prediction_id, case_id, condition, attempt, verdict in prediction_refs:
        run = next(
            row for row in runs
            if (row["case_id"], row["condition"], row["attempt"])
            == (case_id, condition, attempt)
        )
        predictions.append({
            "prediction_id": prediction_id,
            "case_id": case_id,
            "condition": condition,
            "replicate": 1,
            "attempt": attempt,
            "raw_host_output_sha256": run["artifacts"]["raw_host_output_sha256"],
            "verdict": verdict,
        })

    by_condition = {
        condition: {
            "runs_observed": sum(row["condition"] == condition for row in runs),
            "runs_completed": sum(row["condition"] == condition and row["run_status"] == "completed" for row in runs),
            "workflow_completion_rate_observed": (
                sum(row["condition"] == condition and row["run_status"] == "completed" for row in runs)
                / sum(row["condition"] == condition for row in runs)
            ),
            "duration_seconds_mean_known_only": (
                sum(
                    row["duration_seconds"]
                    for row in runs
                    if row["condition"] == condition and isinstance(row["duration_seconds"], (int, float))
                )
                / sum(
                    row["condition"] == condition and isinstance(row["duration_seconds"], (int, float))
                    for row in runs
                )
            ),
            "duration_known_count": sum(
                row["condition"] == condition and isinstance(row["duration_seconds"], (int, float))
                for row in runs
            ),
            "reported_findings_all_attempts": sum(row["reported_finding_count"] for row in runs if row["condition"] == condition),
            "unknown_cost_count": sum(row["condition"] == condition for row in runs),
            "human_precision_mean": None,
            "known_defect_recall_mean": None,
        }
        for condition in ("current_skill", "same_host_direct")
    }
    pair_statuses = (
        ["paired_inputs_verified_host_identity_incomplete_skill_load_unverified"] * 8
        + ["not_comparable"] * 2
        + ["inconclusive_protocol_violation"]
    )
    summary = {
        "schema_version": 1,
        "benchmark_id": "review-changes-real-history-v1",
        "run_count": 23,
        "independent_case_groups": [f"case{i:04d}" for i in range(1, 12)],
        "adjudicated_case_count": 0,
        "historical_recalculation": {
            "status": "retained_historical_descriptive",
            "descriptive_replay_status": "completed",
            "strict_replay_status": "blocked",
            "strict_rescore_status": "blocked",
            "strict_summary_replay_status": "blocked",
            "strict_summary_status": "blocked",
            "raw_host_output_status": "raw_bound_verified",
            "human_adjudication_status": "pending",
        },
        "by_condition": by_condition,
        "interpretation": [],
        "paired_cases": [
            {
                "status": status,
                "repository_snapshot_checks": {
                    condition: {"scoring": "verified"}
                    for condition in conditions
                },
            }
            for status in pair_statuses
        ],
    }
    evidence = {
        "schema_version": 1,
        "benchmark_id": summary["benchmark_id"],
        "run_summary": copy.deepcopy(summary),
            "runs": runs,
        "run_statistics_recomputed_from_records": {
            condition: {
                "completed": by_condition[condition]["runs_completed"],
                "duration_known_count": sum(
                    row["condition"] == condition and isinstance(row["duration_seconds"], (int, float))
                    for row in runs
                ),
                "duration_mean_known_only_seconds": sum(
                    row["duration_seconds"]
                    for row in runs
                    if row["condition"] == condition and isinstance(row["duration_seconds"], (int, float))
                ) / by_condition[condition]["runs_completed"],
                "reported_findings_all_attempts": sum(
                    row["reported_finding_count"] for row in runs if row["condition"] == condition
                ),
                "runs": by_condition[condition]["runs_observed"],
            }
            for condition in ("current_skill", "same_host_direct")
        },
        "agent_provisional_adjudication": {
            "label_source": "agent",
            "status": "provisional",
            "revision": {"number": 2},
            "counts": {"supported": 7, "unsupported": 0, "unresolved": 3, "total_predictions": 10},
            "predictions": predictions,
        },
            "inclusion": {
            "frozen_oracle_included": False,
            "private_mapping_included": False,
            "raw_host_outputs_included": False,
            "source_run_records_included": False,
            },
        "historical_source_verification": {
            "status": "retained_historical_descriptive",
            "run_record_count": 23,
            "descriptive_replay": {
                "status": "completed",
                "run_count": 23,
                "raw_host_outputs_verified_count": 23,
                "basis": "fixture source bytes",
            },
            "raw_host_outputs": {
                "status": "raw_bound_verified",
                "checked_count": 23,
                "matched_count": 23,
                "mismatch_count": 0,
                "finding_arrays_valid_count": 23,
                "binding": "each current host-output file SHA-256 equals the digest in its preserved original run.json",
            },
            "historical_input_binding": {
                "status": "historical_input_binding_unknown",
                "unknown_count": 23,
                "current_copy_digests_are_audit_only": True,
                "note": "Current reviewer-input hashes are observed during export and do not establish the bytes present during the historical run.",
            },
            "strict_replay": {
                "status": "blocked",
                "strict_rescore_status": "blocked",
                "score_attempt_count": 23,
                "score_accepted_count": 0,
                "score_blocked_count": 23,
                "score_validation_error_count": 23,
                "summary_status": "blocked",
                "new_score_files_written": 0,
                "original_score_cache_used": False,
                "legacy_compatibility_results_discarded": 0,
                "blocked_reason": "The preserved original records lack input.review_manifest_sha256; current copied bytes cannot retroactively bind a historical run.",
            },
            "original_scores": {"status": "preserved_only_not_replayed", "file_count": 23},
            "derived_artifact_hash_refresh": {
                "status": "recomputed_from_preserved_source_bytes",
                "run_record_hash_mismatch_count": 0,
                "score_hash_mismatch_count": 0,
                "stale_template_hashes_not_trusted": True,
            },
            "recalculation_basis": {
                "workflow_status": "original run.json run_status values",
                "duration": "original run.json execution.duration_seconds values",
                "reported_findings": "finding arrays in raw host outputs verified against each original record digest",
                "agent_verdicts": "frozen agent adjudication v2 source file; no verdicts changed",
                "pair_statuses": "retained historical classifications from the original summary; not newly strict-scored",
            },
            "replay_tool": {
                "source": "eval/ab_eval.py",
                "version": "test-version",
                "sha256": hashlib.sha256((Path(__file__).parents[1] / "eval" / "ab_eval.py").read_bytes()).hexdigest(),
            },
            "source_manifests": {
                "run_records_sha256": "1" * 64,
                "raw_host_outputs_sha256": "2" * 64,
                "original_scores_sha256": "3" * 64,
            },
        },
        "source_digests": {
            **EXPECTED_SOURCE_HASHES,
            "portable_summary_sha256": "0" * 64,
            "historical_run_records_manifest_sha256": "1" * 64,
            "historical_raw_outputs_manifest_sha256": "2" * 64,
            "historical_original_scores_manifest_sha256": "3" * 64,
            "historical_replay_tool_sha256": hashlib.sha256((Path(__file__).parents[1] / "eval" / "ab_eval.py").read_bytes()).hexdigest(),
        },
        "source_digest_scopes": SOURCE_DIGEST_SCOPES,
    }
    return summary, evidence


def _bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _historical_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, dict[str, str], dict[Path, bytes]]:
    summary, evidence = _fixture()
    runs_root = tmp_path / "runs"
    summary_path = tmp_path / "summary-template.json"
    evidence_path = tmp_path / "evidence-template.json"
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    record_bytes_by_path: dict[Path, bytes] = {}
    rows = {
        (row["case_id"], row["condition"], row["replicate"], row["attempt"]): row
        for row in evidence["runs"]
    }
    raw_hashes: dict[tuple[str, str, int, int], str] = {}
    for row in evidence["runs"]:
        case_id = row["case_id"]
        condition = row["condition"]
        attempt = row["attempt"]
        run_dir = runs_root / case_id / condition / f"repeat{attempt:02d}"
        (run_dir / "artifacts").mkdir(parents=True)
        findings = [{}] * row["reported_finding_count"]
        raw_bytes = _bytes({"findings": findings})
        (run_dir / "host-output.json").write_bytes(raw_bytes)
        (run_dir / "artifacts" / "reviewer-input.json").write_bytes(_bytes({"case": case_id}))
        score_bytes = _bytes({"legacy_score": case_id, "attempt": attempt})
        (run_dir / "score.json").write_bytes(score_bytes)
        identity = (case_id, condition, row["replicate"], attempt)
        raw_digest = hashlib.sha256(raw_bytes).hexdigest()
        raw_hashes[identity] = raw_digest
        record = {
            "case_id": case_id,
            "condition": condition,
            "comparison": {
                "replicate": row["replicate"],
                "attempt": attempt,
                "attempt_kind": "retry" if attempt == 2 else "initial",
                "run_order": len(record_bytes_by_path) + 1,
                "concurrent": False,
            },
            "run_status": row["run_status"],
            "artifacts": {"raw_host_output": "host-output.json", "raw_host_output_sha256": raw_digest},
            "input": {
                "repository_id": "example/repo",
                "review_manifest": "artifacts/reviewer-input.json",
                "review_manifest_sha256": None,
                "review_scope_fingerprint": _sha(f"scope:{case_id}"),
                "snapshot_fingerprint": _sha(f"snapshot:{case_id}"),
                "scope_paths": ["src/example.py"],
            },
            "scoring": {"output": "score.json"},
            "execution": {"duration_seconds": row["duration_seconds"]},
            "annotation": {"status": "pending"},
            "host": {"name": "test-host", "version": None, "model": None, "configuration_fingerprint": None},
            "model_usage": {"cost": None},
        }
        run_path = run_dir / "run.json"
        record_bytes = _bytes(record)
        run_path.write_bytes(record_bytes)
        record_bytes_by_path[run_path] = record_bytes
        projected = rows[identity]
        projected["artifacts"]["raw_host_output_sha256"] = raw_digest
        projected["artifacts"]["run_record_sha256"] = "f" * 64
        projected["artifacts"]["score_sha256"] = "e" * 64
        for prediction in evidence["agent_provisional_adjudication"]["predictions"]:
            if (
                prediction["case_id"],
                prediction["condition"],
                prediction["replicate"],
                prediction["attempt"],
            ) == identity:
                prediction["raw_host_output_sha256"] = raw_digest

    v2_predictions = [
        {
            "prediction": {"prediction_id": prediction["prediction_id"]},
            "adjudication": {"verdict": prediction["verdict"]},
        }
        for prediction in evidence["agent_provisional_adjudication"]["predictions"]
    ]
    v2 = {"cases": [{"predictions": v2_predictions}]}
    source_data = {
        "ab-summary.json": _bytes({"source": "summary"}),
        "adjudication-input.json": _bytes({"source": "input"}),
        "adjudication-result.json": _bytes({"source": "v1"}),
        "adjudication-result-v2.json": _bytes(v2),
    }
    for filename, payload in source_data.items():
        (operator_root / filename).write_bytes(payload)
    expected_hashes = {
        key: hashlib.sha256((operator_root / filename).read_bytes()).hexdigest()
        for key, filename in SOURCE_DIGEST_FILES.items()
    }
    summary_path.write_bytes(_bytes(summary))
    evidence_path.write_bytes(_bytes(evidence))
    preserved_files = {
        path: path.read_bytes()
        for path in runs_root.rglob("*.json")
    }
    return summary_path, evidence_path, runs_root, operator_root, expected_hashes, preserved_files


def test_build_binds_exact_written_summary_bytes_and_embedded_identity(tmp_path: Path) -> None:
    summary, evidence = _fixture()
    summary_source = tmp_path / "summary-source.json"
    evidence_template = tmp_path / "evidence-template.json"
    summary_output = tmp_path / "published-summary.json"
    evidence_output = tmp_path / "published-evidence.json"
    summary_bytes = _bytes(summary)
    summary_source.write_bytes(summary_bytes)
    evidence_template.write_bytes(_bytes(evidence))

    build_export(summary_source, evidence_template, summary_output, evidence_output)

    published_summary = summary_output.read_bytes()
    published_evidence = json.loads(evidence_output.read_bytes())
    assert published_summary == summary_bytes
    assert published_evidence["source_digests"]["portable_summary_sha256"] == hashlib.sha256(published_summary).hexdigest()
    assert published_evidence["run_summary"] == json.loads(published_summary)
    check_export(summary_output, evidence_output)


def test_validation_rejects_byte_drift_even_when_json_is_semantically_equal() -> None:
    summary, evidence = _fixture()
    summary_bytes = _bytes(summary)
    evidence["source_digests"]["portable_summary_sha256"] = hashlib.sha256(summary_bytes).hexdigest()
    validate_export(summary_bytes, _bytes(evidence))

    equivalent_bytes = summary_bytes + b"\n"
    with pytest.raises(ExportError, match="file bytes"):
        validate_export(equivalent_bytes, _bytes(evidence))


def test_validation_rejects_semantic_identity_and_historical_projection_drift() -> None:
    summary, evidence = _fixture()
    summary_bytes = _bytes(summary)
    evidence["source_digests"]["portable_summary_sha256"] = hashlib.sha256(summary_bytes).hexdigest()
    evidence["run_summary"]["run_count"] = 22
    with pytest.raises(ExportError, match="evidence.run_summary"):
        validate_export(summary_bytes, _bytes(evidence))

    summary, evidence = _fixture()
    summary_bytes = _bytes(summary)
    evidence["source_digests"]["portable_summary_sha256"] = hashlib.sha256(summary_bytes).hexdigest()
    evidence["agent_provisional_adjudication"]["counts"]["supported"] = 6
    with pytest.raises(ExportError, match="supported"):
        validate_export(summary_bytes, _bytes(evidence))


def test_check_fails_on_stale_digest(tmp_path: Path) -> None:
    summary, evidence = _fixture()
    summary_path = tmp_path / "summary.json"
    evidence_path = tmp_path / "evidence.json"
    summary_path.write_bytes(_bytes(summary))
    evidence_path.write_bytes(_bytes(evidence))
    with pytest.raises(ExportError, match="file bytes"):
        check_export(summary_path, evidence_path)


def test_build_historical_export_recomputes_only_descriptive_counts(tmp_path: Path) -> None:
    summary_template, evidence_template, runs_root, operator_root, expected_hashes, preserved_files = _historical_fixture(tmp_path)
    summary_output = tmp_path / "out" / "summary.json"
    evidence_output = tmp_path / "out" / "evidence.json"
    with (
        patch("eval.export_ab_pilot.EXPECTED_SOURCE_HASHES", expected_hashes),
        patch("eval.ab_eval.score_record", side_effect=ValueError("missing input.review_manifest_sha256")),
        patch("eval.ab_eval.summarize", side_effect=ValueError("missing input.review_manifest_sha256")),
    ):
        build_historical_export(
            summary_template,
            evidence_template,
            runs_root,
            operator_root,
            summary_output,
            evidence_output,
        )

    summary_bytes = summary_output.read_bytes()
    evidence_bytes = evidence_output.read_bytes()
    summary = json.loads(summary_bytes)
    evidence = json.loads(evidence_bytes)
    assert evidence["run_summary"] == summary
    assert evidence["source_digests"]["portable_summary_sha256"] == hashlib.sha256(summary_bytes).hexdigest()
    assert summary["historical_recalculation"]["status"] == "retained_historical_descriptive"
    assert summary["historical_recalculation"]["descriptive_replay_status"] == "completed"
    assert summary["historical_recalculation"]["strict_rescore_status"] == "blocked"
    assert summary["historical_recalculation"]["strict_summary_replay_status"] == "blocked"
    assert summary["by_condition"]["same_host_direct"]["runs_observed"] == 12
    assert summary["by_condition"]["same_host_direct"]["runs_completed"] == 11
    assert summary["by_condition"]["same_host_direct"]["reported_findings_all_attempts"] == 7
    assert summary["by_condition"]["current_skill"]["runs_observed"] == 11
    assert summary["by_condition"]["current_skill"]["runs_completed"] == 11
    assert summary["by_condition"]["current_skill"]["reported_findings_all_attempts"] == 6
    assert evidence["historical_source_verification"]["raw_host_outputs"]["matched_count"] == 23
    assert evidence["historical_source_verification"]["historical_input_binding"]["unknown_count"] == 23
    assert evidence["historical_source_verification"]["strict_replay"]["score_blocked_count"] == 23
    assert evidence["historical_source_verification"]["strict_replay"]["new_score_files_written"] == 0
    assert evidence["historical_source_verification"]["descriptive_replay"]["status"] == "completed"
    assert evidence["historical_source_verification"]["derived_artifact_hash_refresh"]["run_record_hash_mismatch_count"] == 23
    assert evidence["historical_source_verification"]["derived_artifact_hash_refresh"]["score_hash_mismatch_count"] == 23
    assert all("score_sha256" not in run["artifacts"] for run in evidence["runs"])
    assert all(run["artifacts"]["original_score_status"] == "preserved_only_not_replayed" for run in evidence["runs"])
    for pair in summary["paired_cases"]:
        for condition_checks in pair["repository_snapshot_checks"].values():
            assert condition_checks["scoring"] == "not_replayed"
            assert condition_checks["original_summary_reported_scoring"] == "verified"
            assert condition_checks["strict_rescore"] == "blocked"
    assert {path: path.read_bytes() for path in preserved_files} == preserved_files
    with patch("eval.export_ab_pilot.EXPECTED_SOURCE_HASHES", expected_hashes):
        check_export(summary_output, evidence_output)


def test_build_historical_export_rejects_raw_bytes_that_drift_from_original_record(tmp_path: Path) -> None:
    summary_template, evidence_template, runs_root, operator_root, expected_hashes, _preserved_files = _historical_fixture(tmp_path)
    raw_path = next(runs_root.rglob("host-output.json"))
    raw_path.write_bytes(raw_path.read_bytes() + b" ")
    with (
        patch("eval.export_ab_pilot.EXPECTED_SOURCE_HASHES", expected_hashes),
        patch("eval.ab_eval.score_record", side_effect=ValueError("missing input.review_manifest_sha256")),
        patch("eval.ab_eval.summarize", side_effect=ValueError("missing input.review_manifest_sha256")),
    ):
        with pytest.raises(ExportError, match="raw host-output SHA-256"):
            build_historical_export(
                summary_template,
                evidence_template,
                runs_root,
                operator_root,
                tmp_path / "out-summary.json",
                tmp_path / "out-evidence.json",
            )
    assert not (tmp_path / "out-summary.json").exists()
    assert not (tmp_path / "out-evidence.json").exists()


def test_blind_review_preparation_removes_agent_labels_and_keeps_frozen_sources(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    cases = []
    canonical_fields = ["title", "severity", "root_cause", "trigger", "impact", "introduced_by_change", "evidence", "validation"]
    for case_number in range(1, 12):
        case_key = f"g-{case_number:08x}"
        case_dir = operator_root / "grader-sources" / case_key
        case_dir.mkdir(parents=True)
        (case_dir / "base-head.patch").write_bytes(f"patch {case_number}".encode())
        predictions = []
        if case_number <= 10:
            predictions = [{
                "prediction_id": f"p{case_number:04d}",
                "condition": "current_skill",
                "verdict": "supported",
                "title": "Example finding",
                "severity": "P2",
                "root_cause": "Root cause",
                "trigger": "Trigger",
                "impact": "Impact",
                "introduced_by_change": True,
                "evidence": [],
                "validation": "Probe",
            }]
        case = {
            "case_key": case_key,
            "base": f"base-{case_number}",
            "head": f"head-{case_number}",
            "head_tree": f"tree-{case_number}",
            "scope": "branch",
            "scope_paths": [f"src/{case_number}.py"],
            "scoped_diff_sha256": _sha(f"diff-{case_number}"),
            "predictions": predictions,
            "known_issue_ids": ["hidden-oracle-id"],
        }
        cases.append(case)
    source = {
        "schema_version": 1,
        "benchmark_id": "review-changes-real-history-v1",
        "canonical_finding_fields": canonical_fields,
        "cases": cases,
    }
    anonymous_input = tmp_path / "adjudication-input.json"
    anonymous_input.write_bytes(_bytes(source))
    input_output = tmp_path / "blind-input.json"
    sources_output = tmp_path / "frozen-sources.json"
    template_output = tmp_path / "labels-template.json"
    patched_hashes = {
        **EXPECTED_SOURCE_HASHES,
        "adjudication_input_sha256": hashlib.sha256(anonymous_input.read_bytes()).hexdigest(),
    }

    with patch("eval.export_ab_pilot.EXPECTED_SOURCE_HASHES", patched_hashes):
        prepare_blind_review_bundle(
            anonymous_input,
            operator_root,
            input_output,
            sources_output,
            template_output,
        )

    blind = json.loads(input_output.read_bytes())
    frozen_sources = json.loads(sources_output.read_bytes())
    template = json.loads(template_output.read_bytes())
    assert sum(len(case["predictions"]) for case in blind["cases"]) == 10
    assert "known_issue_ids" not in input_output.read_text(encoding="utf-8")
    assert "verdict" not in input_output.read_text(encoding="utf-8")
    assert all("condition" not in prediction and "verdict" not in prediction for case in blind["cases"] for prediction in case["predictions"])
    assert len(frozen_sources["cases"]) == 11
    assert frozen_sources["cases"][0]["patch_sha256"] == hashlib.sha256(b"patch 1").hexdigest()
    assert template["status"] == "pending"
    assert all(item["verdict"] is None for item in template["prediction_labels"])
    assert all(item["case_classification"] is None for item in template["case_labels"])


def test_build_rejects_changed_operator_source_files(tmp_path: Path) -> None:
    summary, evidence = _fixture()
    summary_path = tmp_path / "summary.json"
    evidence_template = tmp_path / "evidence.json"
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    summary_path.write_bytes(_bytes(summary))
    evidence_template.write_bytes(_bytes(evidence))
    for name in ("ab-summary.json", "adjudication-input.json", "adjudication-result.json", "adjudication-result-v2.json"):
        (operator_root / name).write_text("unfrozen source", encoding="utf-8")

    with pytest.raises(ExportError, match="pinned source file digest changed"):
        build_export(summary_path, evidence_template, tmp_path / "out-summary.json", tmp_path / "out-evidence.json", operator_root)
    assert not (tmp_path / "out-summary.json").exists()
