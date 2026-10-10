#!/usr/bin/env python3
"""Bind the public A/B pilot summary to its evidence export and verify it."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


CONDITIONS = {"same_host_direct", "current_skill"}
EXPECTED_RUNS = 23
EXPECTED_PAIR_STATUSES = {
    "paired_inputs_verified_host_identity_incomplete_skill_load_unverified": 8,
    "not_comparable": 2,
    "inconclusive_protocol_violation": 1,
}
EXPECTED_AGENT_COUNTS = {"supported": 7, "unsupported": 0, "unresolved": 3}
EXPECTED_AGENT_COUNTS_BY_CONDITION = {
    "same_host_direct": {"supported": 4, "unresolved": 1},
    "current_skill": {"supported": 3, "unresolved": 2},
}
SOURCE_DIGEST_FILES = {
    "ab_summary_sha256": "ab-summary.json",
    "adjudication_input_sha256": "adjudication-input.json",
    "agent_adjudication_v1_sha256": "adjudication-result.json",
    "agent_adjudication_v2_sha256": "adjudication-result-v2.json",
}
EXPECTED_SOURCE_HASHES = {
    "ab_summary_sha256": "034c84faad5f1041505b14a4d86db4203aa282bbd5ef38d015181f925a5b475b",
    "adjudication_input_sha256": "5cddda038fd695db0d2809cdf83ca2de54fc3e60422b64446e8a10ce46f4bdc2",
    "agent_adjudication_v1_sha256": "ba4c28a13321d02884cb3e2f6a9b292fbcbb6caa0e2399a927b4d75d8455fe2c",
    "agent_adjudication_v2_sha256": "57d5306874541d2d7e466ae38d2fa1b6b8023365fbfedea67d978f1cb748fce9",
}
SOURCE_DIGEST_SCOPES = {
    "ab_summary_sha256": {
        "source": "review-benchmark-operator-20261009/ab-summary.json",
        "scope": "source_file_bytes",
    },
    "adjudication_input_sha256": {
        "source": "review-benchmark-operator-20261009/adjudication-input.json",
        "scope": "source_file_bytes",
    },
    "agent_adjudication_v1_sha256": {
        "source": "review-benchmark-operator-20261009/adjudication-result.json",
        "scope": "source_file_bytes",
    },
    "agent_adjudication_v2_sha256": {
        "source": "review-benchmark-operator-20261009/adjudication-result-v2.json",
        "scope": "source_file_bytes",
    },
    "portable_summary_sha256": {
        "source": "eval/results/2026-10-09-ab-pilot-summary.json",
        "scope": "portable_file_bytes",
    },
    "historical_run_records_manifest_sha256": {
        "source": "review-benchmark-20261009/runs/**/run.json",
        "scope": "canonical_json_of_relative_paths_and_exact_file_sha256_values",
    },
    "historical_raw_outputs_manifest_sha256": {
        "source": "review-benchmark-20261009/runs/**/host-output.json",
        "scope": "canonical_json_of_relative_paths_and_exact_file_sha256_values",
    },
    "historical_original_scores_manifest_sha256": {
        "source": "review-benchmark-20261009/runs/**/score.json",
        "scope": "canonical_json_of_relative_paths_and_exact_file_sha256_values; preserved_only_not_replayed",
    },
    "historical_replay_tool_sha256": {
        "source": "eval/ab_eval.py",
        "scope": "source_file_bytes",
    },
}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExportError(ValueError):
    """Raised when the summary and evidence do not describe the same pilot."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read JSON {path}: {exc}") from exc


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExportError(message)


def _expected_statistics(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for condition in sorted(CONDITIONS):
        condition_runs = [run for run in runs if run.get("condition") == condition]
        durations = [
            run["duration_seconds"]
            for run in condition_runs
            if _number(run.get("duration_seconds")) and run["duration_seconds"] >= 0
        ]
        result[condition] = {
            "completed": sum(run.get("run_status") == "completed" for run in condition_runs),
            "duration_known_count": len(durations),
            "duration_mean_known_only_seconds": sum(durations) / len(durations) if durations else None,
            "reported_findings_all_attempts": sum(
                run.get("reported_finding_count", 0)
                for run in condition_runs
                if isinstance(run.get("reported_finding_count", 0), int)
                and not isinstance(run.get("reported_finding_count", 0), bool)
            ),
            "runs": len(condition_runs),
        }
    return result


def _expected_summary_statistics(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for condition in sorted(CONDITIONS):
        condition_runs = [run for run in runs if run.get("condition") == condition]
        durations = [
            run["duration_seconds"]
            for run in condition_runs
            if _number(run.get("duration_seconds")) and run["duration_seconds"] >= 0
        ]
        completed = sum(run.get("run_status") == "completed" for run in condition_runs)
        result[condition] = {
            "runs_observed": len(condition_runs),
            "runs_completed": completed,
            "workflow_completion_rate_observed": completed / len(condition_runs) if condition_runs else None,
            "duration_seconds_mean_known_only": sum(durations) / len(durations) if durations else None,
            "duration_known_count": len(durations),
            "reported_findings_all_attempts": sum(
                run.get("reported_finding_count", 0)
                for run in condition_runs
                if isinstance(run.get("reported_finding_count", 0), int)
                and not isinstance(run.get("reported_finding_count", 0), bool)
            ),
            "unknown_cost_count": sum(
                run.get("model_usage", {}).get("cost") is None for run in condition_runs
            ),
        }
    return result


def _compare_statistics(actual: Any, expected: dict[str, dict[str, Any]]) -> None:
    _require(isinstance(actual, dict), "evidence.run_statistics_recomputed_from_records must be an object")
    _require(set(actual) == CONDITIONS, "run statistics must contain exactly the A and B conditions")
    for condition, expected_values in expected.items():
        actual_values = actual.get(condition)
        _require(isinstance(actual_values, dict), f"run statistics for {condition} must be an object")
        for key, expected_value in expected_values.items():
            actual_value = actual_values.get(key)
            if isinstance(expected_value, float):
                _require(
                    _number(actual_value)
                    and math.isclose(actual_value, expected_value, rel_tol=1e-12, abs_tol=1e-9),
                    f"run statistic {condition}.{key} disagrees with evidence.runs",
                )
            else:
                _require(
                    actual_value == expected_value,
                    f"run statistic {condition}.{key} disagrees with evidence.runs",
                )


def _manifest_sha256(entries: list[dict[str, str]]) -> str:
    ordered = sorted(entries, key=lambda item: item["path"])
    return _sha256(_json_bytes(ordered))


def _run_identity(record: dict[str, Any]) -> tuple[str, str, int, int]:
    comparison = record.get("comparison")
    _require(isinstance(comparison, dict), "historical run record comparison metadata is missing")
    case_id = record.get("case_id")
    condition = record.get("condition")
    replicate = comparison.get("replicate")
    attempt = comparison.get("attempt")
    _require(isinstance(case_id, str) and case_id, "historical run record case_id is missing")
    _require(condition in CONDITIONS, f"historical run record has an unknown condition: {condition}")
    _require(isinstance(replicate, int) and not isinstance(replicate, bool), "historical replicate is invalid")
    _require(isinstance(attempt, int) and not isinstance(attempt, bool), "historical attempt is invalid")
    return case_id, condition, replicate, attempt


def _run_reference(run_dir: Path, reference: Any, label: str) -> Path:
    _require(isinstance(reference, str) and reference, f"historical {label} path is missing")
    candidate = Path(reference)
    resolved = candidate.resolve() if candidate.is_absolute() else (run_dir / candidate).resolve()
    try:
        resolved.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise ExportError(f"historical {label} path escapes its run directory: {reference}") from exc
    _require(resolved.is_file(), f"historical {label} file is missing: {reference}")
    return resolved


def _historical_summary(
    template: dict[str, Any],
    records: list[dict[str, Any]],
    raw_finding_counts: dict[tuple[str, str, int, int], int],
) -> dict[str, Any]:
    summary = copy.deepcopy(template)
    case_sources: dict[str, str] = {}
    for record in records:
        case_id = record["case_id"]
        repository_id = record.get("input", {}).get("repository_id")
        _require(isinstance(repository_id, str) and repository_id, f"repository_id is missing for {case_id}")
        previous = case_sources.setdefault(case_id, repository_id)
        _require(previous == repository_id, f"repository_id changes across runs for {case_id}")
    summary["run_count"] = len(records)
    summary["independent_case_groups"] = sorted(case_sources)
    summary["case_origin"] = "real_repository"
    summary["case_source_counts"] = dict(sorted(Counter(case_sources.values()).items()))
    summary["adjudicated_case_count"] = 0
    summary["pending_annotation_case_count"] = len(case_sources)

    by_condition: dict[str, dict[str, Any]] = {}
    for condition in sorted(CONDITIONS):
        condition_records = [record for record in records if record.get("condition") == condition]
        completed = sum(record.get("run_status") == "completed" for record in condition_records)
        durations = [
            record.get("execution", {}).get("duration_seconds")
            for record in condition_records
            if _number(record.get("execution", {}).get("duration_seconds"))
            and record["execution"]["duration_seconds"] >= 0
        ]
        by_condition[condition] = {
            "runs_observed": len(condition_records),
            "runs_completed": completed,
            "workflow_completion_rate_observed": completed / len(condition_records) if condition_records else None,
            "duration_seconds_mean_known_only": sum(durations) / len(durations) if durations else None,
            "duration_known_count": len(durations),
            "human_precision_mean": None,
            "human_precision_case_count": 0,
            "known_defect_recall_mean": None,
            "known_defect_recall_case_count": 0,
            "unknown_cost_count": sum(
                record.get("model_usage", {}).get("cost") is None for record in condition_records
            ),
            "reported_findings_all_attempts": sum(
                raw_finding_counts[_run_identity(record)] for record in condition_records
            ),
        }
    summary["by_condition"] = by_condition
    summary["historical_recalculation"] = {
        "status": "retained_historical_descriptive",
        "descriptive_replay_status": "completed",
        "strict_replay_status": "blocked",
        "strict_rescore_status": "blocked",
        "strict_summary_replay_status": "blocked",
        "raw_host_output_status": "raw_bound_verified",
        "human_adjudication_status": "pending",
    }
    for pair in summary.get("paired_cases", []):
        if not isinstance(pair, dict):
            continue
        checks = pair.get("repository_snapshot_checks")
        if not isinstance(checks, dict):
            continue
        for condition_checks in checks.values():
            if isinstance(condition_checks, dict) and "scoring" in condition_checks:
                condition_checks["original_summary_reported_scoring"] = condition_checks["scoring"]
                condition_checks["scoring"] = "not_replayed"
                condition_checks["strict_rescore"] = "blocked"
    interpretations = summary.get("interpretation")
    _require(isinstance(interpretations, list) and all(isinstance(item, str) for item in interpretations), "summary interpretation must be an array of strings")
    added = [
        "Historical workflow counts were recalculated from original run-record metadata and raw host-output bytes whose SHA-256 matched the original records.",
        "Strict score and summary replay is blocked because the original records do not bind reviewer-input bytes; hashes of current copies are audit-only and were not backfilled.",
        "Original score files were preserved by hash but were not used as a replay cache; no new score files were produced.",
    ]
    summary["interpretation"] = list(dict.fromkeys([*interpretations, *added]))
    return summary


def _validate_agent_projection(evidence: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    adjudication = evidence.get("agent_provisional_adjudication")
    _require(isinstance(adjudication, dict), "evidence must retain agent_provisional_adjudication")
    _require(adjudication.get("label_source") == "agent", "agent projection must remain agent-sourced")
    _require(adjudication.get("status") == "provisional", "agent projection must remain provisional")
    _require(adjudication.get("revision", {}).get("number") == 2, "agent projection must remain v2")
    counts = adjudication.get("counts")
    _require(isinstance(counts, dict), "agent projection counts are missing")
    for key, value in EXPECTED_AGENT_COUNTS.items():
        _require(counts.get(key) == value, f"agent projection count {key} must remain {value}")
    _require(counts.get("total_predictions") == 10, "agent projection must retain all 10 predictions")
    predictions = adjudication.get("predictions")
    _require(isinstance(predictions, list) and len(predictions) == 10, "agent projection must retain 10 predictions")
    by_condition: dict[str, Counter[str]] = {condition: Counter() for condition in CONDITIONS}
    run_keys = {
        (
            run.get("case_id"),
            run.get("condition"),
            run.get("replicate"),
            run.get("attempt"),
            run.get("artifacts", {}).get("raw_host_output_sha256"),
        )
        for run in runs
    }
    prediction_ids: set[str] = set()
    for prediction in predictions:
        _require(isinstance(prediction, dict), "agent prediction entries must be objects")
        prediction_id = prediction.get("prediction_id")
        _require(isinstance(prediction_id, str) and prediction_id not in prediction_ids, "prediction IDs must be unique")
        prediction_ids.add(prediction_id)
        condition = prediction.get("condition")
        verdict = prediction.get("verdict")
        _require(condition in CONDITIONS, f"prediction {prediction_id} has an unknown condition")
        _require(verdict in EXPECTED_AGENT_COUNTS, f"prediction {prediction_id} has an unknown verdict")
        by_condition[condition][verdict] += 1
        key = (
            prediction.get("case_id"),
            condition,
            prediction.get("replicate"),
            prediction.get("attempt"),
            prediction.get("raw_host_output_sha256"),
        )
        _require(key in run_keys, f"prediction {prediction_id} does not bind to a preserved run digest")
    for condition, expected_counts in EXPECTED_AGENT_COUNTS_BY_CONDITION.items():
        _require(
            {key: by_condition[condition][key] for key in ("supported", "unresolved")} == expected_counts,
            f"agent projection for {condition} must remain {expected_counts}",
        )


def _validate_historical_provenance(summary: dict[str, Any], evidence: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    summary_status = summary.get("historical_recalculation")
    _require(isinstance(summary_status, dict), "summary must mark its historical recalculation basis")
    _require(summary_status.get("status") == "retained_historical_descriptive", "summary must remain historical descriptive evidence")
    _require(summary_status.get("descriptive_replay_status") == "completed", "summary must mark descriptive replay completed")
    _require(summary_status.get("strict_rescore_status") == "blocked", "summary must mark strict rescore blocked")
    _require(summary_status.get("strict_summary_replay_status") == "blocked", "summary must mark strict summary replay blocked")
    _require(summary_status.get("strict_replay_status") == "blocked", "summary must disclose the blocked strict replay")
    _require(summary_status.get("raw_host_output_status") == "raw_bound_verified", "summary must disclose raw-output verification")
    _require(summary_status.get("human_adjudication_status") == "pending", "summary must keep human adjudication pending")

    provenance = evidence.get("historical_source_verification")
    _require(isinstance(provenance, dict), "evidence.historical_source_verification is required")
    _require(provenance.get("status") == "retained_historical_descriptive", "historical source verification status is invalid")
    _require(provenance.get("run_record_count") == EXPECTED_RUNS, "historical source verification must retain 23 records")
    descriptive = provenance.get("descriptive_replay")
    _require(isinstance(descriptive, dict), "descriptive replay provenance is missing")
    _require(descriptive.get("status") == "completed", "descriptive replay must be marked completed")
    _require(descriptive.get("run_count") == EXPECTED_RUNS, "descriptive replay must retain all 23 records")
    _require(descriptive.get("raw_host_outputs_verified_count") == EXPECTED_RUNS, "descriptive replay must use all byte-verified raw outputs")
    raw = provenance.get("raw_host_outputs")
    _require(isinstance(raw, dict), "historical raw host-output verification is missing")
    _require(raw.get("status") == "raw_bound_verified", "raw host outputs must be byte-verified against their original record hashes")
    _require(raw.get("checked_count") == EXPECTED_RUNS and raw.get("matched_count") == EXPECTED_RUNS, "all 23 raw host outputs must match their original record hashes")
    _require(raw.get("mismatch_count") == 0, "raw host-output hash mismatches are not permitted")
    _require(raw.get("finding_arrays_valid_count") == EXPECTED_RUNS, "all raw host outputs must contain a findings array")

    input_binding = provenance.get("historical_input_binding")
    _require(isinstance(input_binding, dict), "historical input binding status is missing")
    _require(input_binding.get("status") == "historical_input_binding_unknown", "historical manifest digest status must remain unknown")
    _require(input_binding.get("unknown_count") == EXPECTED_RUNS, "all 23 historical reviewer-input digests must remain unknown")
    _require(input_binding.get("current_copy_digests_are_audit_only") is True, "current manifest hashes must remain audit-only")

    replay = provenance.get("strict_replay")
    _require(isinstance(replay, dict), "strict replay provenance is missing")
    _require(replay.get("status") == "blocked", "strict replay must remain blocked")
    _require(replay.get("strict_rescore_status") == "blocked", "strict rescore must be explicitly marked blocked")
    _require(replay.get("score_attempt_count") == EXPECTED_RUNS, "strict replay must attempt all 23 records")
    _require(replay.get("score_accepted_count") == 0, "no new historical score may be accepted")
    _require(replay.get("score_blocked_count") == EXPECTED_RUNS, "all 23 strict scores must be blocked")
    _require(
        replay.get("summary_status") in {"blocked", "blocked_legacy_unbound_result_discarded"},
        "strict summary replay must remain blocked",
    )
    _require(replay.get("new_score_files_written") == 0, "historical descriptive export must not write new score files")
    _require(replay.get("original_score_cache_used") is False, "original score files must not be used as a replay cache")

    original_scores = provenance.get("original_scores")
    _require(isinstance(original_scores, dict), "original score preservation record is missing")
    _require(original_scores.get("status") == "preserved_only_not_replayed", "original scores must be labeled preserved-only")
    _require(original_scores.get("file_count") == EXPECTED_RUNS, "all 23 original score files must be preserved by hash")

    refresh = provenance.get("derived_artifact_hash_refresh")
    _require(isinstance(refresh, dict), "derived artifact hash refresh audit is missing")
    _require(refresh.get("status") == "recomputed_from_preserved_source_bytes", "derived artifact hashes must come from preserved source bytes")
    for key in ("run_record_hash_mismatch_count", "score_hash_mismatch_count"):
        value = refresh.get(key)
        _require(isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= EXPECTED_RUNS, f"derived artifact refresh count {key} is invalid")
    _require(
        refresh.get("stale_template_hashes_not_trusted") is True,
        "stale template run/score hashes must not override preserved source bytes",
    )

    tool = provenance.get("replay_tool")
    _require(isinstance(tool, dict), "replay tool provenance is missing")
    _require(tool.get("source") == "eval/ab_eval.py", "historical replay tool source is invalid")
    _require(isinstance(tool.get("version"), str) and tool.get("version"), "historical replay tool version is missing")
    tool_digest = tool.get("sha256")
    _require(isinstance(tool_digest, str) and HEX_SHA256.fullmatch(tool_digest), "historical replay tool digest is invalid")
    _require(tool_digest == _sha256(Path(__file__).with_name("ab_eval.py").read_bytes()), "historical replay tool source digest changed")

    source_manifests = provenance.get("source_manifests")
    _require(isinstance(source_manifests, dict), "historical source manifests are missing")
    digest_fields = {
        "run_records_sha256": "historical_run_records_manifest_sha256",
        "raw_host_outputs_sha256": "historical_raw_outputs_manifest_sha256",
        "original_scores_sha256": "historical_original_scores_manifest_sha256",
    }
    for manifest_name, digest_name in digest_fields.items():
        digest = source_manifests.get(manifest_name)
        _require(isinstance(digest, str) and HEX_SHA256.fullmatch(digest), f"historical source manifest {manifest_name} is invalid")
        _require(evidence.get("source_digests", {}).get(digest_name) == digest, f"source digest {digest_name} differs from its verification manifest")
    _require(evidence.get("source_digests", {}).get("historical_replay_tool_sha256") == tool_digest, "replay tool source digest is inconsistent")

    for run in runs:
        artifacts = run.get("artifacts")
        _require(isinstance(artifacts, dict), "historical run artifacts are missing")
        _require("score_sha256" not in artifacts, "new score cache digests must not appear in historical evidence")
        for field in ("run_record_sha256", "raw_host_output_sha256", "original_score_sha256"):
            value = artifacts.get(field)
            _require(isinstance(value, str) and HEX_SHA256.fullmatch(value), f"historical artifact digest {field} is invalid")
        _require(artifacts.get("original_score_status") == "preserved_only_not_replayed", "original score must not be treated as a valid replay cache")
        audit = run.get("review_manifest_digest_audit")
        _require(isinstance(audit, dict), "current reviewer-input digest audit is missing")
        _require(audit.get("historical_status") == "historical_input_binding_unknown", "current digest must not fill historical input binding")
        _require(audit.get("scope") == "current_copy_bytes_observed_at_export", "current reviewer-input digest scope is invalid")
        _require(isinstance(audit.get("current_audit_digest"), str) and HEX_SHA256.fullmatch(audit["current_audit_digest"]), "current reviewer-input audit digest is invalid")


def validate_export(summary_bytes: bytes, evidence_bytes: bytes) -> None:
    try:
        summary = json.loads(summary_bytes)
        evidence = json.loads(evidence_bytes)
    except json.JSONDecodeError as exc:
        raise ExportError(f"summary or evidence is invalid JSON: {exc}") from exc
    _require(isinstance(summary, dict), "summary must be an object")
    _require(isinstance(evidence, dict), "evidence must be an object")
    _require(summary.get("schema_version") == 1, "unsupported summary schema version")
    _require(evidence.get("schema_version") == 1, "unsupported evidence schema version")
    benchmark_id = summary.get("benchmark_id")
    _require(isinstance(benchmark_id, str) and benchmark_id, "summary benchmark_id is required")
    _require(evidence.get("benchmark_id") == benchmark_id, "summary and evidence benchmark_id differ")
    _require(evidence.get("run_summary") == summary, "evidence.run_summary differs from the published summary")
    digests = evidence.get("source_digests")
    _require(isinstance(digests, dict), "evidence.source_digests is required")
    _require(
        digests.get("portable_summary_sha256") == _sha256(summary_bytes),
        "portable_summary_sha256 does not match the summary file bytes",
    )
    _require(all(isinstance(value, str) and HEX_SHA256.fullmatch(value) for value in digests.values()), "source digests must be lowercase SHA-256 values")
    for key, expected_digest in EXPECTED_SOURCE_HASHES.items():
        _require(digests.get(key) == expected_digest, f"pinned source digest changed: {key}")
    _require(
        evidence.get("source_digest_scopes") == SOURCE_DIGEST_SCOPES,
        "source_digest_scopes must identify each source file and byte scope",
    )

    runs = evidence.get("runs")
    _require(isinstance(runs, list) and len(runs) == EXPECTED_RUNS, f"evidence must retain all {EXPECTED_RUNS} attempts")
    run_keys = [
        (run.get("case_id"), run.get("condition"), run.get("replicate"), run.get("attempt"))
        for run in runs
        if isinstance(run, dict)
    ]
    _require(len(run_keys) == EXPECTED_RUNS and len(set(run_keys)) == EXPECTED_RUNS, "evidence run identities must be present and unique")
    _require(all(key[1] in CONDITIONS for key in run_keys), "evidence contains an unknown run condition")
    _compare_statistics(evidence.get("run_statistics_recomputed_from_records"), _expected_statistics(runs))

    by_condition = summary.get("by_condition")
    _require(isinstance(by_condition, dict) and set(by_condition) == CONDITIONS, "summary must contain exactly the A and B conditions")
    summary_statistics = _expected_summary_statistics(runs)
    for condition in CONDITIONS:
        values = by_condition[condition]
        _require(isinstance(values, dict), f"summary condition {condition} must be an object")
        for key, expected_value in summary_statistics[condition].items():
            actual_value = values.get(key)
            if isinstance(expected_value, float):
                _require(
                    _number(actual_value)
                    and math.isclose(actual_value, expected_value, rel_tol=1e-12, abs_tol=1e-9),
                    f"summary statistic {condition}.{key} disagrees with historical evidence",
                )
            else:
                _require(actual_value == expected_value, f"summary statistic {condition}.{key} disagrees with historical evidence")
    _require(summary.get("run_count") == EXPECTED_RUNS, "summary must retain all 23 attempts")
    _require(len(summary.get("independent_case_groups", [])) == 11, "summary must retain all 11 case groups")
    statuses = Counter(pair.get("status") for pair in summary.get("paired_cases", []) if isinstance(pair, dict))
    _require(dict(statuses) == EXPECTED_PAIR_STATUSES, "summary pair-status counts drifted from the historical pilot")
    _require(summary.get("adjudicated_case_count") == 0, "human adjudication must remain pending")
    _require(
        all(
            by_condition[condition].get("human_precision_mean") is None
            and by_condition[condition].get("known_defect_recall_mean") is None
            for condition in CONDITIONS
        ),
        "human quality metrics must remain unknown until human labels exist",
    )
    _validate_historical_provenance(summary, evidence, runs)
    inclusion = evidence.get("inclusion")
    _require(isinstance(inclusion, dict), "evidence.inclusion is required")
    for key in ("frozen_oracle_included", "private_mapping_included", "raw_host_outputs_included", "source_run_records_included"):
        _require(inclusion.get(key) is False, f"evidence must keep {key} excluded")
    _validate_agent_projection(evidence, runs)


def _stage(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
        return Path(stream.name)


def build_export(
    summary_source: Path,
    evidence_template: Path,
    summary_output: Path,
    evidence_output: Path,
    operator_root: Path | None = None,
) -> None:
    summary_bytes = summary_source.read_bytes()
    summary = json.loads(summary_bytes)
    evidence = _load_json(evidence_template)
    if not isinstance(summary, dict) or not isinstance(evidence, dict):
        raise ExportError("summary and evidence template must be JSON objects")
    if summary.get("benchmark_id") != evidence.get("benchmark_id"):
        raise ExportError("summary and evidence template benchmark_id differ")
    digests = evidence.get("source_digests")
    if not isinstance(digests, dict):
        raise ExportError("evidence template has no source_digests object")
    if operator_root is not None:
        for key, filename in SOURCE_DIGEST_FILES.items():
            source_path = operator_root / filename
            source_digest = _sha256(source_path.read_bytes())
            if source_digest != EXPECTED_SOURCE_HASHES[key]:
                raise ExportError(f"pinned source file digest changed: {source_path}")
            digests[key] = source_digest
    evidence["run_summary"] = summary
    digests["portable_summary_sha256"] = _sha256(summary_bytes)
    evidence["source_digest_scopes"] = SOURCE_DIGEST_SCOPES
    evidence_bytes = _json_bytes(evidence)
    validate_export(summary_bytes, evidence_bytes)

    staged: list[tuple[Path, Path]] = []
    try:
        staged.append((_stage(summary_output, summary_bytes), summary_output))
        staged.append((_stage(evidence_output, evidence_bytes), evidence_output))
        for temporary, destination in staged:
            temporary.replace(destination)
        validate_export(summary_output.read_bytes(), evidence_output.read_bytes())
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def build_historical_export(
    summary_template: Path,
    evidence_template: Path,
    runs_root: Path,
    operator_root: Path,
    summary_output: Path,
    evidence_output: Path,
) -> None:
    if __package__:
        from .ab_eval import VERSION as SCORER_VERSION
        from .ab_eval import score_record, summarize
    else:
        from ab_eval import VERSION as SCORER_VERSION
        from ab_eval import score_record, summarize

    summary = _load_json(summary_template)
    evidence = _load_json(evidence_template)
    _require(isinstance(summary, dict) and isinstance(evidence, dict), "historical templates must be JSON objects")
    _require(summary.get("benchmark_id") == evidence.get("benchmark_id"), "historical templates have different benchmark IDs")

    source_hashes: dict[str, str] = {}
    for key, filename in SOURCE_DIGEST_FILES.items():
        source_path = operator_root / filename
        digest = _sha256(source_path.read_bytes())
        _require(digest == EXPECTED_SOURCE_HASHES[key], f"pinned source file digest changed: {source_path}")
        source_hashes[key] = digest

    adjudication_v2 = _load_json(operator_root / "adjudication-result-v2.json")
    _require(isinstance(adjudication_v2, dict) and isinstance(adjudication_v2.get("cases"), list), "frozen v2 agent adjudication is invalid")
    frozen_verdicts: dict[str, str] = {}
    for case in adjudication_v2["cases"]:
        _require(isinstance(case, dict) and isinstance(case.get("predictions"), list), "frozen v2 agent case is invalid")
        for item in case["predictions"]:
            _require(isinstance(item, dict), "frozen v2 prediction is invalid")
            prediction = item.get("prediction")
            adjudication = item.get("adjudication")
            _require(isinstance(prediction, dict) and isinstance(adjudication, dict), "frozen v2 prediction or adjudication is invalid")
            prediction_id = prediction.get("prediction_id")
            verdict = adjudication.get("verdict")
            _require(isinstance(prediction_id, str) and prediction_id not in frozen_verdicts, "frozen v2 prediction ID is missing or duplicated")
            _require(verdict in EXPECTED_AGENT_COUNTS, f"frozen v2 verdict is invalid for {prediction_id}")
            frozen_verdicts[prediction_id] = verdict
    projected_predictions = evidence.get("agent_provisional_adjudication", {}).get("predictions")
    _require(isinstance(projected_predictions, list), "evidence agent projection is missing")
    projected_verdicts = {item.get("prediction_id"): item.get("verdict") for item in projected_predictions if isinstance(item, dict)}
    _require(projected_verdicts == frozen_verdicts and len(frozen_verdicts) == 10, "evidence predictions differ from the frozen v2 agent verdicts")

    runs_root = runs_root.resolve()
    operator_root = operator_root.resolve()
    run_paths = sorted(runs_root.rglob("run.json"))
    _require(len(run_paths) == EXPECTED_RUNS, f"historical source must contain exactly {EXPECTED_RUNS} run records")
    source_rows = evidence.get("runs")
    _require(isinstance(source_rows, list), "evidence run projection is missing")
    source_row_map: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for row in source_rows:
        _require(isinstance(row, dict), "historical evidence run projection must contain objects")
        key = (row.get("case_id"), row.get("condition"), row.get("replicate"), row.get("attempt"))
        _require(key not in source_row_map, "historical evidence run identity is duplicated")
        source_row_map[key] = row
    _require(len(source_row_map) == EXPECTED_RUNS, "historical evidence run projection must retain all 23 attempts")

    records: list[dict[str, Any]] = []
    projected_runs: list[dict[str, Any]] = []
    raw_finding_counts: dict[tuple[str, str, int, int], int] = {}
    record_manifest_entries: list[dict[str, str]] = []
    raw_manifest_entries: list[dict[str, str]] = []
    score_manifest_entries: list[dict[str, str]] = []
    missing_manifest_binding_count = 0
    legacy_compatibility_outputs_discarded = 0
    stale_run_record_hash_count = 0
    stale_score_hash_count = 0

    for run_path in run_paths:
        run_bytes = run_path.read_bytes()
        record = json.loads(run_bytes)
        _require(isinstance(record, dict), f"historical run record must be an object: {run_path}")
        identity = _run_identity(record)
        _require(identity not in raw_finding_counts, f"historical run identity is duplicated: {identity}")
        source_row = source_row_map.get(identity)
        _require(source_row is not None, f"historical evidence is missing run identity: {identity}")

        record_digest = _sha256(run_bytes)
        raw_path = _run_reference(run_path.parent, record.get("artifacts", {}).get("raw_host_output"), "raw host output")
        raw_bytes = raw_path.read_bytes()
        raw_digest = _sha256(raw_bytes)
        bound_raw_digest = record.get("artifacts", {}).get("raw_host_output_sha256")
        _require(isinstance(bound_raw_digest, str) and HEX_SHA256.fullmatch(bound_raw_digest), f"original raw host-output digest is missing: {run_path}")
        _require(raw_digest == bound_raw_digest, f"raw host-output SHA-256 differs from the original record: {run_path}")
        raw_output = json.loads(raw_bytes)
        _require(isinstance(raw_output, dict) and isinstance(raw_output.get("findings"), list), f"raw host output has no findings array: {run_path}")
        finding_count = len(raw_output["findings"])
        raw_finding_counts[identity] = finding_count

        manifest_ref = record.get("input", {}).get("review_manifest")
        manifest_path = _run_reference(run_path.parent, manifest_ref, "reviewer input")
        current_manifest_digest = _sha256(manifest_path.read_bytes())
        recorded_manifest_digest = record.get("input", {}).get("review_manifest_sha256")
        if recorded_manifest_digest is None:
            missing_manifest_binding_count += 1
        else:
            _require(
                isinstance(recorded_manifest_digest, str)
                and HEX_SHA256.fullmatch(recorded_manifest_digest)
                and recorded_manifest_digest == current_manifest_digest,
                f"recorded reviewer-input digest does not match its referenced bytes: {run_path}",
            )

        score_path = _run_reference(run_path.parent, record.get("scoring", {}).get("output", "score.json"), "original score")
        score_digest = _sha256(score_path.read_bytes())
        source_artifacts = source_row.get("artifacts", {})
        _require(isinstance(source_artifacts, dict), f"historical evidence artifacts are missing: {identity}")
        expected_run_digest = source_artifacts.get("run_record_sha256")
        if expected_run_digest is not None and expected_run_digest != record_digest:
            stale_run_record_hash_count += 1
        expected_raw_digest = source_artifacts.get("raw_host_output_sha256")
        if expected_raw_digest is not None:
            _require(expected_raw_digest == raw_digest, f"historical evidence raw_host_output_sha256 differs from its source bytes: {identity}")
        expected_score_digest = source_artifacts.get("score_sha256", source_artifacts.get("original_score_sha256"))
        if expected_score_digest is not None and expected_score_digest != score_digest:
            stale_score_hash_count += 1

        relative_path = run_path.relative_to(runs_root.resolve()).as_posix()
        record_manifest_entries.append({"path": relative_path, "sha256": record_digest})
        raw_relative = raw_path.relative_to(runs_root.resolve()).as_posix()
        raw_manifest_entries.append({"path": raw_relative, "sha256": raw_digest})
        score_relative = score_path.relative_to(runs_root.resolve()).as_posix()
        score_manifest_entries.append({"path": score_relative, "sha256": score_digest})

        comparison = record["comparison"]
        execution = record.get("execution", {})
        projected = copy.deepcopy(source_row)
        projected.update({
            "case_id": identity[0],
            "condition": identity[1],
            "replicate": identity[2],
            "attempt": identity[3],
            "attempt_kind": comparison.get("attempt_kind", source_row.get("attempt_kind")),
            "run_order": comparison.get("run_order", source_row.get("run_order")),
            "concurrent": comparison.get("concurrent", source_row.get("concurrent")),
            "run_status": record.get("run_status"),
            "duration_seconds": execution.get("duration_seconds"),
            "duration_source": "host_duration_seconds" if _number(execution.get("duration_seconds")) else None,
            "reported_finding_count": finding_count,
            "annotation_status": record.get("annotation", {}).get("status"),
            "host": {
                key: record.get("host", {}).get(key)
                for key in ("name", "version", "model", "configuration_fingerprint")
            },
            "input": {
                key: record.get("input", {}).get(key)
                for key in ("repository_id", "review_scope_fingerprint", "snapshot_fingerprint", "scope_paths")
            },
            "review_manifest_digest_audit": {
                "historical_status": "historical_input_binding_unknown" if recorded_manifest_digest is None else "historical_input_binding_verified",
                "current_audit_digest": current_manifest_digest,
                "scope": "current_copy_bytes_observed_at_export",
            },
        })
        projected_artifacts = dict(source_artifacts)
        projected_artifacts.pop("score_sha256", None)
        projected_artifacts.update({
            "raw_host_output_included": False,
            "raw_host_output_sha256": raw_digest,
            "run_record_sha256": record_digest,
            "original_score_sha256": score_digest,
            "original_score_status": "preserved_only_not_replayed",
        })
        projected["artifacts"] = projected_artifacts
        projected["model_usage"] = {"cost": record.get("model_usage", {}).get("cost")}
        projected_runs.append(projected)
        records.append(record)

    _require(missing_manifest_binding_count == EXPECTED_RUNS, "pilot export expects all 23 historical reviewer-input digests to remain unknown")

    strict_score_errors = 0
    for run_path in run_paths:
        try:
            score_record(run_path)
        except Exception:
            strict_score_errors += 1
        else:
            legacy_compatibility_outputs_discarded += 1
    strict_summary_status = "blocked"
    try:
        summarize(run_paths, sorted({record["case_id"] for record in records}))
    except Exception:
        pass
    else:
        strict_summary_status = "blocked_legacy_unbound_result_discarded"
        legacy_compatibility_outputs_discarded += 1

    old_pair_statuses = Counter(
        pair.get("status") for pair in summary.get("paired_cases", []) if isinstance(pair, dict)
    )
    _require(dict(old_pair_statuses) == EXPECTED_PAIR_STATUSES, "historical pair-status counts differ from the frozen pilot")
    summary = _historical_summary(summary, records, raw_finding_counts)
    summary["historical_recalculation"]["strict_summary_status"] = strict_summary_status
    summary["historical_recalculation"]["strict_summary_replay_status"] = "blocked"

    run_manifest_digest = _manifest_sha256(record_manifest_entries)
    raw_manifest_digest = _manifest_sha256(raw_manifest_entries)
    score_manifest_digest = _manifest_sha256(score_manifest_entries)
    replay_tool_digest = _sha256(Path(__file__).with_name("ab_eval.py").read_bytes())
    evidence["runs"] = projected_runs
    evidence["run_statistics_recomputed_from_records"] = _expected_statistics(projected_runs)
    evidence["historical_source_verification"] = {
        "status": "retained_historical_descriptive",
        "run_record_count": len(records),
        "descriptive_replay": {
            "status": "completed",
            "run_count": len(records),
            "raw_host_outputs_verified_count": len(records),
            "basis": "original run-record workflow metadata plus findings arrays in raw outputs bound by recorded SHA-256",
        },
        "raw_host_outputs": {
            "status": "raw_bound_verified",
            "checked_count": len(records),
            "matched_count": len(records),
            "mismatch_count": 0,
            "finding_arrays_valid_count": len(records),
            "binding": "each current host-output file SHA-256 equals the digest in its preserved original run.json",
        },
        "historical_input_binding": {
            "status": "historical_input_binding_unknown",
            "unknown_count": missing_manifest_binding_count,
            "current_copy_digests_are_audit_only": True,
            "note": "Current reviewer-input hashes are observed during export and do not establish the bytes present during the historical run.",
        },
        "strict_replay": {
            "status": "blocked",
            "strict_rescore_status": "blocked",
            "score_attempt_count": len(records),
            "score_accepted_count": 0,
            "score_blocked_count": len(records),
            "score_validation_error_count": strict_score_errors,
            "summary_status": strict_summary_status,
            "new_score_files_written": 0,
            "original_score_cache_used": False,
            "legacy_compatibility_results_discarded": legacy_compatibility_outputs_discarded,
            "blocked_reason": "The preserved original records lack input.review_manifest_sha256; current copied bytes cannot retroactively bind a historical run.",
        },
        "original_scores": {
            "status": "preserved_only_not_replayed",
            "file_count": len(score_manifest_entries),
        },
        "derived_artifact_hash_refresh": {
            "status": "recomputed_from_preserved_source_bytes",
            "run_record_hash_mismatch_count": stale_run_record_hash_count,
            "score_hash_mismatch_count": stale_score_hash_count,
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
            "version": SCORER_VERSION,
            "sha256": replay_tool_digest,
        },
        "source_manifests": {
            "run_records_sha256": run_manifest_digest,
            "raw_host_outputs_sha256": raw_manifest_digest,
            "original_scores_sha256": score_manifest_digest,
        },
    }
    digests = evidence.get("source_digests")
    _require(isinstance(digests, dict), "historical evidence template has no source_digests object")
    digests.update({
        "historical_run_records_manifest_sha256": run_manifest_digest,
        "historical_raw_outputs_manifest_sha256": raw_manifest_digest,
        "historical_original_scores_manifest_sha256": score_manifest_digest,
        "historical_replay_tool_sha256": replay_tool_digest,
    })
    evidence["source_digest_scopes"] = SOURCE_DIGEST_SCOPES

    with tempfile.TemporaryDirectory(prefix="ab-pilot-historical-") as temporary_dir:
        temporary = Path(temporary_dir)
        generated_summary = temporary / "summary.json"
        generated_evidence = temporary / "evidence.json"
        generated_summary.write_bytes(_json_bytes(summary))
        generated_evidence.write_bytes(_json_bytes(evidence))
        build_export(
            generated_summary,
            generated_evidence,
            summary_output,
            evidence_output,
            operator_root=operator_root,
        )


def check_export(summary_path: Path, evidence_path: Path) -> None:
    validate_export(summary_path.read_bytes(), evidence_path.read_bytes())


def prepare_blind_review_bundle(
    anonymous_input: Path,
    operator_root: Path,
    input_output: Path,
    sources_output: Path,
    template_output: Path,
) -> None:
    source_bytes = anonymous_input.read_bytes()
    source = json.loads(source_bytes)
    if not isinstance(source, dict) or not isinstance(source.get("cases"), list):
        raise ExportError("anonymous adjudication input must contain a cases array")
    _require(
        _sha256(source_bytes) == EXPECTED_SOURCE_HASHES["adjudication_input_sha256"],
        "anonymous adjudication input digest differs from the frozen source",
    )
    _require(source.get("benchmark_id") == "review-changes-real-history-v1", "unexpected benchmark ID")
    canonical_fields = source.get("canonical_finding_fields")
    _require(isinstance(canonical_fields, list) and all(isinstance(field, str) for field in canonical_fields), "canonical finding fields are missing")
    _require(len(source["cases"]) == 11, "blind review source index must retain all 11 cases")

    review_cases: list[dict[str, Any]] = []
    frozen_cases: list[dict[str, Any]] = []
    case_labels: list[dict[str, Any]] = []
    prediction_labels: list[dict[str, Any]] = []
    prediction_count = 0
    for case in source["cases"]:
        _require(isinstance(case, dict), "anonymous source case must be an object")
        case_key = case.get("case_key")
        _require(isinstance(case_key, str) and case_key, "anonymous source case has no case_key")
        patch_relative = Path("grader-sources") / case_key / "base-head.patch"
        checkout_relative = Path("grader-sources") / case_key / "head"
        patch_path = operator_root / patch_relative
        _require(patch_path.is_file(), f"frozen patch is missing: {patch_path}")
        frozen_cases.append({
            "case_key": case_key,
            "base": case.get("base"),
            "head": case.get("head"),
            "head_tree": case.get("head_tree"),
            "scope": case.get("scope"),
            "scope_paths": case.get("scope_paths"),
            "scoped_diff_sha256": case.get("scoped_diff_sha256"),
            "patch_file": patch_relative.as_posix(),
            "patch_sha256": _sha256(patch_path.read_bytes()),
            "head_checkout": checkout_relative.as_posix(),
        })
        case_labels.append({"case_key": case_key, "case_classification": None, "known_findings": None, "rationale": None})
        predictions = case.get("predictions", [])
        _require(isinstance(predictions, list), f"predictions for {case_key} must be an array")
        case_predictions: list[dict[str, Any]] = []
        for prediction in predictions:
            _require(isinstance(prediction, dict), "anonymous prediction must be an object")
            prediction_id = prediction.get("prediction_id")
            _require(isinstance(prediction_id, str) and prediction_id, "anonymous prediction has no ID")
            prediction_body = {field: prediction[field] for field in canonical_fields if field in prediction}
            _require(set(prediction_body) == set(canonical_fields), f"prediction {prediction_id} is missing canonical fields")
            case_predictions.append({"prediction_id": prediction_id, "prediction": prediction_body})
            prediction_labels.append({
                "prediction_id": prediction_id,
                "verdict": None,
                "matches_known_findings": None,
                "duplicate_of": None,
                "rationale": None,
            })
            prediction_count += 1
        review_cases.append({"case_key": case_key, "predictions": case_predictions})
    _require(prediction_count == 10, "blind review input must retain exactly 10 predictions")
    blind_input = {
        "schema_version": 1,
        "benchmark_id": source["benchmark_id"],
        "source_sha256": _sha256(source_bytes),
        "cases": review_cases,
    }
    sources = {
        "schema_version": 1,
        "benchmark_id": source["benchmark_id"],
        "source_input": "review-benchmark-operator-20261009/adjudication-input.json",
        "source_input_sha256": _sha256(source_bytes),
        "cases": frozen_cases,
    }
    template = {
        "schema_version": 1,
        "benchmark_id": source["benchmark_id"],
        "label_source": "human",
        "status": "pending",
        "reviewer_id": None,
        "reviewed_at": None,
        "case_labels": case_labels,
        "prediction_labels": prediction_labels,
    }
    output_payloads = (
        (input_output, _json_bytes(blind_input)),
        (sources_output, _json_bytes(sources)),
        (template_output, _json_bytes(template)),
    )
    _require(len({path.resolve() for path, _ in output_payloads}) == 3, "blind review output paths must be different")
    staged: list[tuple[Path, Path]] = []
    try:
        for destination, payload in output_payloads:
            staged.append((_stage(destination, payload), destination))
        for temporary, destination in staged:
            temporary.replace(destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Write a summary and matching evidence export")
    build.add_argument("--summary", type=Path, required=True, help="Summary generated from saved run records")
    build.add_argument("--evidence-template", type=Path, required=True, help="Preserved evidence source to bind")
    build.add_argument("--operator-root", type=Path, required=True, help="Directory containing the pinned source summary and v1/v2 adjudication files")
    build.add_argument("--summary-output", type=Path, required=True)
    build.add_argument("--evidence-output", type=Path, required=True)
    historical = subparsers.add_parser(
        "build-historical",
        help="Rebuild historical descriptive counts from bound raw outputs without reusing old scores",
    )
    historical.add_argument("--summary-template", type=Path, required=True)
    historical.add_argument("--evidence-template", type=Path, required=True)
    historical.add_argument("--runs-root", type=Path, required=True)
    historical.add_argument("--operator-root", type=Path, required=True)
    historical.add_argument("--summary-output", type=Path, required=True)
    historical.add_argument("--evidence-output", type=Path, required=True)
    check = subparsers.add_parser("check", help="Check byte and semantic identity of an export")
    check.add_argument("--summary", type=Path, required=True)
    check.add_argument("--evidence", type=Path, required=True)
    blind = subparsers.add_parser("prepare-blind-review", help="Prepare condition-blind input, frozen source index, and empty labels template")
    blind.add_argument("--anonymous-input", type=Path, required=True)
    blind.add_argument("--operator-root", type=Path, required=True)
    blind.add_argument("--input-output", type=Path, required=True)
    blind.add_argument("--sources-output", type=Path, required=True)
    blind.add_argument("--template-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build_export(
                args.summary,
                args.evidence_template,
                args.summary_output,
                args.evidence_output,
                operator_root=args.operator_root,
            )
            print(f"verified summary/evidence export: {args.summary_output} {args.evidence_output}")
            return 0
        if args.command == "build-historical":
            build_historical_export(
                args.summary_template,
                args.evidence_template,
                args.runs_root,
                args.operator_root,
                args.summary_output,
                args.evidence_output,
            )
            print(f"built retained historical descriptive export: {args.summary_output} {args.evidence_output}")
            return 0
        if args.command == "prepare-blind-review":
            prepare_blind_review_bundle(
                args.anonymous_input,
                args.operator_root,
                args.input_output,
                args.sources_output,
                args.template_output,
            )
            print(f"prepared condition-blind review bundle: {args.input_output} {args.sources_output} {args.template_output}")
            return 0
        check_export(args.summary, args.evidence)
        print(f"verified summary/evidence export: {args.summary} {args.evidence}")
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"export_ab_pilot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
