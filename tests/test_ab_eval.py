from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from eval.ab_eval import (
    _canonical_bytes,
    _protocol_incidents,
    _read_lines,
    _repository_snapshot_check,
    build_run_record,
    score_record,
    summarize,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
    ).stdout


def _repository_snapshot(repository: Path, base: str, head: str) -> dict[str, str]:
    return {
        "base_tree": _git(repository, "rev-parse", f"{base}^{{tree}}").decode().strip(),
        "head_tree": _git(repository, "rev-parse", f"{head}^{{tree}}").decode().strip(),
        "scoped_diff_sha256": hashlib.sha256(
            _git(repository, "diff", "--binary", base, head)
        ).hexdigest(),
    }


def _make_repository(path: Path) -> tuple[str, str, dict[str, str]]:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    source = path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("before\n", encoding="utf-8")
    _git(path, "add", "src/app.py")
    _git(path, "commit", "--quiet", "-m", "base")
    base = _git(path, "rev-parse", "HEAD").decode().strip()
    source.write_text("after\nsecond line\n", encoding="utf-8")
    _git(path, "add", "src/app.py")
    _git(path, "commit", "--quiet", "-m", "head")
    head = _git(path, "rev-parse", "HEAD").decode().strip()
    return base, head, _repository_snapshot(path, base, head)


def _build_case(tmp_path: Path, *, condition: str = "same_host_direct") -> tuple[dict, dict, Path]:
    input_path = tmp_path / "blind" / "case0001" / "reviewer-input.json"
    reviewer_input = {
        "scope": "branch",
        "base": "base-revision",
        "head": "head-revision",
        "scope_paths": ["src/app.py"],
        "snapshot": {"base_tree": "base-tree", "head_tree": "head-tree"},
    }
    _write_json(input_path, reviewer_input)
    case = {
        "case_id": "case0001",
        "case_origin": "real_repository",
        "source_repository": "example/project",
        "base": "base-revision",
        "head": "head-revision",
        "scope": "branch",
        "scope_paths": ["src/app.py"],
        "snapshot": reviewer_input["snapshot"],
        "reviewer_input": str(input_path),
        "annotation": str(tmp_path / "operator" / "case0001.json"),
        "repositories": {condition: None},
    }
    index = {
        "benchmark_id": "test-benchmark",
        "blind_root": str(input_path.parent.parent),
        "operator_root": str(tmp_path / "operator"),
    }
    run_dir = tmp_path / "runs" / "case0001" / condition / "repeat01"
    run_dir.mkdir(parents=True)
    return index, case, run_dir


def _build_record(
    index: dict,
    case: dict,
    run_dir: Path,
    *,
    condition: str = "same_host_direct",
    skill_root: Path | None = None,
    skill_bundle_path: Path | None = None,
    skill_bundle_sha256: str | None = None,
) -> dict:
    return build_run_record(
        index,
        case,
        condition,
        run_dir,
        blind_root=Path(index["blind_root"]),
        operator_root=Path(index["operator_root"]),
        skill_root=skill_root or run_dir / "missing-skill",
        run_order=1,
        concurrent=False,
        planned_replicates=1,
        order_seed="test-seed",
        skill_label="test-skill",
        skill_revision="f" * 40,
        skill_bundle_sha256=skill_bundle_sha256,
        skill_bundle_path=skill_bundle_path,
    )


def _score_fixture(
    run_dir: Path,
    *,
    case_id: str = "case0001",
    finding_ids: tuple[str, ...] = ("finding-1",),
    run_status: str = "completed",
    read_paths: list[str] | None = None,
    review_status: str = "completed",
    snapshot_fingerprint: str = "same-snapshot",
    recheck: dict | None = None,
    condition: str = "same_host_direct",
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    findings = [
        {
            "finding_id": finding_id,
            "root_cause": "A concrete root cause.",
            "trigger": "A concrete trigger.",
            "impact": "A concrete impact.",
            "introduced_by_change": True,
            "evidence": [],
        }
        for finding_id in finding_ids
    ]
    _write_json(run_dir / "validated-result.json", {"findings": findings})
    _write_json(run_dir / "host-output.json", {"findings": findings})
    if recheck is not None:
        _write_json(run_dir / "recheck.json", recheck)
    record = {
        "schema_version": 1,
        "benchmark_id": "test-benchmark",
        "case_id": case_id,
        "case_origin": "real_repository",
        "condition": condition,
        "run_status": run_status,
        "host": {"name": None, "version": None, "model": None, "configuration_fingerprint": None},
        "environment": {"os": None, "architecture": None},
        "input": {
            "repository_id": "example/project",
            "snapshot_fingerprint": snapshot_fingerprint,
            "review_scope_fingerprint": "same-scope",
            "scope_paths": ["src/app.py"],
            "review_manifest": None,
        },
        "artifacts": {
            "raw_host_output": "host-output.json",
            "validated_result": "validated-result.json",
            "recheck_json": "recheck.json" if recheck is not None else None,
        },
        "annotation": {"status": "pending", "oracle": None, "human_labels": None, "provisional_labels": None},
        "scoring": {"status": "not_run", "script": None, "version": None, "output": None},
        "execution": {
            "failure_reason": "host failed" if run_status == "failed" else None,
            "review_status": review_status,
            "coverage_reported": read_paths is not None,
            "scope_verified": True,
            "coverage_audit": None,
            "read_paths": read_paths,
            "uncovered_paths": [] if read_paths is not None else None,
            "context_omissions": [] if read_paths is not None else None,
            "manual_interventions": None,
            "duration_seconds": None,
        },
        "model_usage": {"input_tokens": None, "output_tokens": None, "cost": None, "currency": None},
        "comparison": {
            "group_id": f"test-benchmark:{case_id}:replicate-1",
            "case_group_id": case_id,
            "replicate": 1,
            "attempt": int(run_dir.name.replace("repeat", "")),
            "attempt_kind": "initial" if run_dir.name == "repeat01" else "retry",
            "run_order": 1,
        },
    }
    path = run_dir / "run.json"
    _write_json(path, record)
    return path


def test_build_preserves_raw_output_and_keeps_operator_counts_unknown_without_events(tmp_path: Path) -> None:
    index, case, run_dir = _build_case(tmp_path)
    raw = (
        b'{\r\n  "run_status": "completed",\r\n  "review_status": "completed",\r\n'
        b'  "scope": {"base": "base-revision", "head": "head-revision", "paths": ["src/app.py"]},\r\n'
        b'  "read_paths": ["src/app.py"], "uncovered_paths": [], "context_omissions": [],\r\n'
        b'  "host": {}, "model_usage": {}}\r\n'
    )
    (run_dir / "host-output.json").write_bytes(raw)
    _write_json(run_dir / "operator-events.json", {
        "verified": True,
        "events": [{"actor": "agent", "kind": "corrected_scope_path", "count": 1}],
    })

    record = _build_record(index, case, run_dir)

    assert (run_dir / "host-output.json").read_bytes() == raw
    assert record["artifacts"]["raw_host_output_sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["host"]["model"] is None
    assert record["model_usage"]["cost"] is None
    assert record["execution"]["host_reported_manual_interventions"] is None
    assert record["execution"]["manual_interventions"] is None
    assert record["execution"]["operator_event_count"] == 1
    assert record["execution"]["manual_intervention_source"] is None


def test_build_normalizes_a_b_condition_alias_without_changing_raw_output(tmp_path: Path) -> None:
    index, case, run_dir = _build_case(tmp_path, condition="current_skill")
    raw = json.dumps(
        {"condition": "B", "run_status": "completed", "findings": []},
        ensure_ascii=False,
    ).encode("utf-8")
    (run_dir / "host-output.json").write_bytes(raw)

    record = _build_record(index, case, run_dir, condition="current_skill")

    assert (run_dir / "host-output.json").read_bytes() == raw
    assert record["condition"] == "current_skill"
    assert record["artifacts"]["raw_host_output_sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["execution"]["normalization_events"] == [
        {
            "field": "condition",
            "raw_value": "B",
            "normalized_value": "current_skill",
            "rule": "A/B alias mapped to the run-directory condition",
        }
    ]


def test_build_rejects_condition_alias_that_disagrees_with_run_directory(tmp_path: Path) -> None:
    index, case, run_dir = _build_case(tmp_path)
    (run_dir / "host-output.json").write_text(
        json.dumps({"condition": "B", "run_status": "completed"}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="condition does not match"):
        _build_record(index, case, run_dir)


def test_pinned_skill_bundle_is_preserved_and_digest_checked(tmp_path: Path) -> None:
    index, case, run_dir = _build_case(tmp_path, condition="current_skill")
    (run_dir / "host-output.json").write_text(
        json.dumps({"run_status": "completed", "skill_bundle_sha256": ""}), encoding="utf-8"
    )
    skill_root = tmp_path / "skill-source"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text("test skill\n", encoding="utf-8")
    bundle = tmp_path / "pinned-skill.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("review-changes/SKILL.md", "test skill\n")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    host = json.loads((run_dir / "host-output.json").read_text(encoding="utf-8"))
    host["skill_bundle_sha256"] = digest
    _write_json(run_dir / "host-output.json", host)

    record = _build_record(
        index,
        case,
        run_dir,
        condition="current_skill",
        skill_root=skill_root,
        skill_bundle_path=bundle,
        skill_bundle_sha256=digest,
    )

    saved_bundle = run_dir / record["artifacts"]["skill_bundle"]
    assert hashlib.sha256(saved_bundle.read_bytes()).hexdigest() == digest
    assert record["skill_provenance"]["bundle_sha256"] == digest
    assert record["skill_provenance"]["verified_loaded"] is True
    assert record["skill_provenance"]["provided_bundle_sha256"] == digest
    assert record["skill_provenance"]["host_reported_bundle_sha256"] == digest


def test_repository_snapshot_checks_and_evidence_reads_use_pinned_git_objects(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    base, head, snapshot = _make_repository(repository)
    fingerprint = hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()

    build_check = _repository_snapshot_check(
        repository, base, head, snapshot, fingerprint, case_input_matches=True
    )

    assert build_check["status"] == "verified"
    assert build_check["worktree_status"] == "clean"
    assert build_check["checks"]["full_diff_matches"] is True
    (repository / "src" / "app.py").write_text("mutable worktree\n", encoding="utf-8")
    scoring_check = _repository_snapshot_check(
        repository, base, head, snapshot, fingerprint, case_input_matches=True
    )

    assert scoring_check["status"] == "mismatch"
    assert scoring_check["worktree_status"] == "dirty"
    assert _read_lines(repository, base, head, {"side": "after", "path": "src/app.py"}) == 2
    assert _read_lines(repository, base, head, {"side": "before", "path": "src/app.py"}) == 1
    assert _read_lines(repository, base, head, {"side": "after", "path": "../escape.py"}) is None


def test_annotation_only_marks_adjudicated_for_a_human_label_file(tmp_path: Path) -> None:
    index, case, run_dir = _build_case(tmp_path)
    operator_root = Path(index["operator_root"])
    annotation_path = Path(case["annotation"])
    _write_json(annotation_path, {"human_label_status": "adjudicated", "human_labels": "agent-labels.json"})
    _write_json(operator_root / "agent-labels.json", {
        "label_source": "agent",
        "status": "adjudicated",
        "prediction_labels": [],
    })
    (run_dir / "host-output.json").write_text(json.dumps({"run_status": "completed"}), encoding="utf-8")
    case["human_label_status"] = "adjudicated"

    record = _build_record(index, case, run_dir)

    assert record["annotation"]["status"] == "reviewed"
    assert record["annotation"]["human_labels"] == "../../../../operator/agent-labels.json"
    _write_json(operator_root / "agent-labels.json", {
        "label_source": "human",
        "status": "adjudicated",
        "prediction_labels": [],
    })

    human_record = _build_record(index, case, run_dir)

    assert human_record["annotation"]["status"] == "adjudicated"
    assert human_record["annotation"]["human_labels"] == "../../../../operator/agent-labels.json"


def test_duplicate_human_prediction_labels_are_rejected_as_invalid(tmp_path: Path) -> None:
    run_dir = tmp_path / "run" / "repeat01"
    run_path = _score_fixture(run_dir)
    _write_json(run_dir / "labels.json", {
        "label_source": "human",
        "status": "adjudicated",
        "case_classification": "defect",
        "known_findings": [],
        "prediction_labels": [
            {"finding_id": "finding-1", "verdict": "unresolved", "matches_known_findings": []},
            {"finding_id": "finding-1", "verdict": "valid", "matches_known_findings": []},
        ],
    })
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["annotation"].update({"status": "adjudicated", "human_labels": "labels.json"})
    _write_json(run_path, record)

    score = score_record(run_path)

    assert score["human_adjudication"]["status"] == "adjudicated_labels_incomplete_or_invalid"
    assert score["human_adjudication"]["precision"] is None


def test_self_reported_coverage_does_not_prove_coverage_or_defect_recall(tmp_path: Path) -> None:
    run_dir = tmp_path / "run" / "repeat01"
    run_path = _score_fixture(run_dir, finding_ids=("finding-1", "finding-2"), read_paths=["src/app.py"])
    _write_json(run_dir / "labels.json", {
        "label_source": "human",
        "status": "adjudicated",
        "case_classification": "defect",
        "known_findings": [{"id": "known-1"}],
        "prediction_labels": [
            {"finding_id": "finding-1", "verdict": "valid", "matches_known_findings": ["known-1"]},
            {"finding_id": "finding-2", "verdict": "unresolved", "matches_known_findings": []},
        ],
    })
    _write_json(run_dir / "operator.json", {
        "candidate_issue": {"id": "known-1"},
        "provisional_candidate_matches": [{"finding_id": "finding-1"}],
    })
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["annotation"] = {
        "status": "adjudicated",
        "oracle": None,
        "human_labels": "labels.json",
        "provisional_labels": "operator.json",
    }
    _write_json(run_path, record)

    score = score_record(run_path)

    assert score["workflow"]["coverage_complete"] is None
    assert score["workflow"]["reported_coverage_complete_unverified"] is True
    assert score["workflow"]["read_paths_evidence"] == "host_reported_unverified"
    assert score["human_adjudication"]["known_defect_recall"] is None
    assert score["human_adjudication"]["wrong_resolved_count"] is None
    assert score["provisional_evidence"]["additional_findings_pending"] == ["finding-2"]
    assert score["model_usage"]["cost"] is None


def test_failed_run_with_no_reported_reads_has_unknown_verified_coverage(tmp_path: Path) -> None:
    run_path = _score_fixture(
        tmp_path / "failed" / "repeat01",
        run_status="failed",
        review_status="failed",
        read_paths=[],
    )

    score = score_record(run_path)

    assert score["workflow"]["coverage_complete"] is None
    assert score["workflow"]["reported_coverage_complete_unverified"] is False
    assert score["workflow"]["execution_completed"] is False


def test_summary_pairs_latest_completed_retry_and_rejects_snapshot_mismatch(tmp_path: Path) -> None:
    base = tmp_path / "runs"
    direct_failed = _score_fixture(
        base / "case0001" / "same_host_direct" / "repeat01",
        case_id="case0001",
        run_status="failed",
        review_status="failed",
        read_paths=[],
    )
    direct_retry = _score_fixture(
        base / "case0001" / "same_host_direct" / "repeat02",
        case_id="case0001",
        read_paths=["src/app.py"],
    )
    skill = _score_fixture(
        base / "case0001" / "current_skill" / "repeat01",
        case_id="case0001",
        read_paths=["src/app.py"],
        condition="current_skill",
    )
    mismatch_direct = _score_fixture(
        base / "case0002" / "same_host_direct" / "repeat01",
        case_id="case0002",
        snapshot_fingerprint="snapshot-a",
        read_paths=["src/app.py"],
    )
    mismatch_skill = _score_fixture(
        base / "case0002" / "current_skill" / "repeat01",
        case_id="case0002",
        snapshot_fingerprint="snapshot-b",
        read_paths=["src/app.py"],
        condition="current_skill",
    )

    report = summarize(
        [direct_failed, direct_retry, skill, mismatch_direct, mismatch_skill],
        ["case0001", "case0002"],
    )

    case1 = next(pair for pair in report["paired_cases"] if pair["case_id"] == "case0001")
    case2 = next(pair for pair in report["paired_cases"] if pair["case_id"] == "case0002")
    assert report["run_count"] == 5
    assert case1["selected_attempts"]["same_host_direct"] == 2
    assert [entry["run_status"] for entry in case1["attempt_history"]["same_host_direct"]] == ["failed", "completed"]
    assert case2["status"] == "not_comparable"


def _prepare_pair_summary_record(run_path: Path, *, condition: str, protocol_violation: bool = False) -> None:
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["host"] = {
        "name": "same-host",
        "version": "1",
        "model": "model",
        "configuration_fingerprint": "configuration",
    }
    record["repository_snapshot"] = {"build_check": {"status": "verified"}}
    record["execution"]["protocol_incidents"] = (
        [{"incident_id": "incident", "raw_host_output_sha256_matches": True}]
        if protocol_violation
        else []
    )
    if condition == "current_skill":
        record["skill_provenance"] = {
            "provided_bundle_sha256": "a" * 64,
            "bundle_sha256": "a" * 64,
            "host_reported_bundle_sha256": None,
            "host_reported_bundle_digest_status": "missing",
            "verified_loaded": None,
        }
    _write_json(run_path, record)
    score = score_record(run_path)
    score["workflow"]["repository_snapshot_check"]["status"] = "verified"
    score["workflow"]["input_fingerprint_check"] = {"status": "verified"}
    _write_json(run_path.parent / "score.json", score)


def test_summary_limits_pair_status_when_skill_load_is_unverified(tmp_path: Path) -> None:
    direct = _score_fixture(
        tmp_path / "runs" / "case0001" / "same_host_direct" / "repeat01",
    )
    skill = _score_fixture(
        tmp_path / "runs" / "case0001" / "current_skill" / "repeat01",
        condition="current_skill",
    )
    _prepare_pair_summary_record(direct, condition="same_host_direct")
    _prepare_pair_summary_record(skill, condition="current_skill")

    pair = summarize([direct, skill], ["case0001"])["paired_cases"][0]

    assert pair["status"] == "paired_inputs_and_host_identity_verified_skill_load_unverified"
    assert pair["skill_load_check"]["verified_loaded"] is None
    assert pair["skill_load_check"]["provided_bundle_sha256"] == "a" * 64
    assert pair["skill_load_check"]["host_reported_bundle_sha256"] is None


def test_summary_marks_digest_matched_protocol_incident_inconclusive(tmp_path: Path) -> None:
    direct = _score_fixture(
        tmp_path / "runs" / "case0010" / "same_host_direct" / "repeat01",
        case_id="case0010",
    )
    skill = _score_fixture(
        tmp_path / "runs" / "case0010" / "current_skill" / "repeat01",
        case_id="case0010",
        condition="current_skill",
    )
    _prepare_pair_summary_record(direct, condition="same_host_direct")
    _prepare_pair_summary_record(
        skill, condition="current_skill", protocol_violation=True
    )

    pair = summarize([direct, skill], ["case0010"])["paired_cases"][0]

    assert pair["status"] == "inconclusive_protocol_violation"
    assert pair["protocol_incidents"][0]["raw_host_output_sha256_matches"] is True


def test_protocol_incident_preserves_restoration_attribution(tmp_path: Path) -> None:
    operator_root = tmp_path / "operator"
    incidents_dir = operator_root / "incidents"
    incidents_dir.mkdir(parents=True)
    run_dir = tmp_path / "runs" / "case0010" / "current_skill" / "repeat01"
    run_dir.mkdir(parents=True)
    host_output = run_dir / "host-output.json"
    host_output.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(host_output.read_bytes()).hexdigest()
    _write_json(
        incidents_dir / "case0010.json",
        {
            "incident_id": "case0010-template",
            "event": "source_template_modified_protocol_violation_and_restored",
            "raw_host_output": str(host_output),
            "raw_host_output_sha256": digest,
            "restoration_attribution": {
                "actor": "agent",
                "role": "coordinator_finisher",
                "phase": "after_review",
            },
        },
    )

    incidents = _protocol_incidents(operator_root, run_dir, host_output)

    assert incidents[0]["raw_host_output_sha256_matches"] is True
    assert incidents[0]["restoration_attribution"] == {
        "actor": "agent",
        "role": "coordinator_finisher",
        "phase": "after_review",
    }
