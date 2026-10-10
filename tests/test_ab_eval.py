from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

import eval.ab_eval as ab_eval
from eval.ab_eval import (
    _canonical_bytes,
    _protocol_incidents,
    _read_lines,
    _repository_snapshot_check,
    build_run_record,
    build_runs,
    main,
    score_record,
    summarize,
    validate_record,
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


def _prepare_build_runs(tmp_path: Path) -> tuple[Path, Path, dict, Path]:
    index, case, run_dir = _build_case(tmp_path)
    _write_json(run_dir / "host-output.json", {"run_status": "completed", "findings": []})
    index["cases"] = [case]
    index_path = tmp_path / "index.json"
    _write_json(index_path, index)
    return index_path, tmp_path / "runs", case, run_dir


def _run_builds(index_path: Path, runs_root: Path) -> list[Path]:
    return build_runs(
        index_path,
        runs_root,
        skill_root=runs_root.parent / "missing-skill",
        planned_replicates=1,
        order_seed="test-seed",
        skill_label="test-skill",
        skill_revision=None,
        skill_bundle_sha256=None,
        skill_bundle_path=None,
    )


def _score_fixture(
    run_dir: Path,
    *,
    case_id: str = "case0001",
    finding_ids: tuple[str, ...] = ("finding-1",),
    run_status: str = "completed",
    read_paths: list[str] | None = None,
    review_status: str = "completed",
    snapshot_fingerprint: str | None = None,
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
    snapshot = {
        "base_tree": "base-tree",
        "head_tree": "head-tree",
        "scoped_diff_sha256": "0" * 64,
    }
    reviewer_input = {
        "scope": "branch",
        "base": "base-revision",
        "head": "head-revision",
        "scope_paths": ["src/app.py"],
        "snapshot": snapshot,
    }
    manifest_path = run_dir / "reviewer-input.json"
    _write_json(manifest_path, reviewer_input)
    actual_snapshot_fingerprint = hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()
    recorded_snapshot_fingerprint = snapshot_fingerprint or actual_snapshot_fingerprint
    validated_path = run_dir / "validated-result.json"
    raw_path = run_dir / "host-output.json"
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
            "review_manifest": "reviewer-input.json",
            "review_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "snapshot_fingerprint": recorded_snapshot_fingerprint,
            "review_scope_fingerprint": hashlib.sha256(_canonical_bytes({
                "scope": reviewer_input["scope"],
                "base": reviewer_input["base"],
                "head": reviewer_input["head"],
                "paths": reviewer_input["scope_paths"],
            })).hexdigest(),
            "scope_paths": ["src/app.py"],
        },
        "artifacts": {
            "raw_transcript": None,
            "raw_transcript_sha256": None,
            "raw_host_output": "host-output.json",
            "raw_host_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "validated_result": "validated-result.json",
            "validated_result_sha256": hashlib.sha256(validated_path.read_bytes()).hexdigest(),
            "markdown_report": None,
            "recheck_json": "recheck.json" if recheck is not None else None,
            "recheck_markdown": None,
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
            "planned_replicates": 1,
            "attempt": int(run_dir.name.replace("repeat", "")),
            "attempt_kind": "initial" if run_dir.name == "repeat01" else "retry",
            "run_order": 1,
            "order_seed": "test-seed",
            "concurrent": False,
            "metadata_artifact": None,
        },
        "repository_snapshot": {
            "base_revision": "base-revision",
            "head_revision": "head-revision",
            "snapshot": snapshot,
            "snapshot_fingerprint": actual_snapshot_fingerprint,
            "case_and_reviewer_input_match": True,
            "build_check": {
                "status": "unverified",
                "checks": {},
                "expected": {},
                "observed": {},
                "worktree_status": "unavailable",
                "limitation": "test fixture",
            },
        },
        "source_paths": {"repository": None, "blind_root": None, "operator_annotation": None},
    }
    path = run_dir / "run.json"
    _write_json(path, record)
    return path


def _persist_score(run_path: Path) -> dict:
    score = score_record(run_path)
    record = json.loads(run_path.read_text(encoding="utf-8"))
    score_path = run_path.parent / "score.json"
    _write_json(score_path, score)
    record["scoring"] = {
        "status": "completed",
        "script": "eval/ab_eval.py",
        "version": score["scorer"]["version"],
        "output": "score.json",
        "dependency_manifest_version": 1,
        "dependency_fingerprint": score["dependencies"]["fingerprint"],
    }
    _write_json(run_path, record)
    return score


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
    repository = run_path.parents[4] / "pair-repository"
    if not (repository / ".git").is_dir():
        _make_repository(repository)
    base = _git(repository, "rev-list", "--max-parents=0", "HEAD").decode().strip()
    head = _git(repository, "rev-parse", "HEAD").decode().strip()
    snapshot = _repository_snapshot(repository, base, head)
    snapshot_fingerprint = hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()
    reviewer_input = {
        "scope": "branch",
        "base": base,
        "head": head,
        "scope_paths": ["src/app.py"],
        "snapshot": snapshot,
    }
    manifest_path = run_path.parent / "reviewer-input.json"
    _write_json(manifest_path, reviewer_input)
    record["input"].update({
        "review_manifest": "reviewer-input.json",
        "review_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "snapshot_fingerprint": snapshot_fingerprint,
        "review_scope_fingerprint": hashlib.sha256(_canonical_bytes({
            "scope": reviewer_input["scope"],
            "base": base,
            "head": head,
            "paths": reviewer_input["scope_paths"],
        })).hexdigest(),
        "scope_paths": ["src/app.py"],
    })
    record["host"] = {
        "name": "same-host",
        "version": "1",
        "model": "model",
        "configuration_fingerprint": "configuration",
    }
    record["repository_snapshot"] = {
        "base_revision": base,
        "head_revision": head,
        "snapshot": snapshot,
        "snapshot_fingerprint": snapshot_fingerprint,
        "case_and_reviewer_input_match": True,
        "build_check": {
            "status": "verified",
            "checks": {},
            "expected": {},
            "observed": {},
            "worktree_status": "clean",
            "limitation": "test fixture",
        },
    }
    record["source_paths"]["repository"] = str(repository)
    if protocol_violation:
        incident_path = run_path.parent / "incident.json"
        _write_json(incident_path, {"incident_id": "incident"})
        record["execution"]["protocol_incidents"] = [{
            "artifact": "incident.json",
            "sha256": hashlib.sha256(incident_path.read_bytes()).hexdigest(),
            "incident_id": "incident",
            "event": "protocol_violation",
            "raw_host_output_sha256_matches": True,
        }]
    else:
        record["execution"]["protocol_incidents"] = []
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
    _write_json(run_path.parent / "score.json", score)
    record["scoring"] = {
        "status": "completed",
        "script": "eval/ab_eval.py",
        "version": score["scorer"]["version"],
        "output": "score.json",
        "dependency_manifest_version": 1,
        "dependency_fingerprint": score["dependencies"]["fingerprint"],
    }
    _write_json(run_path, record)


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


@pytest.mark.parametrize(
    "artifact_name",
    ["host-output.json", "validated-result.json", "reviewer-input.json"],
)
def test_invalid_immutable_artifact_cannot_replace_a_completed_score(
    tmp_path: Path, artifact_name: str
) -> None:
    run_path = _score_fixture(tmp_path / "run" / "repeat01")
    _persist_score(run_path)
    score_path = run_path.parent / "score.json"
    previous_score = score_path.read_bytes()
    previous_record = run_path.read_bytes()
    artifact_path = run_path.parent / artifact_name
    artifact_path.write_bytes(artifact_path.read_bytes() + b"\n")

    errors = validate_record(json.loads(run_path.read_text(encoding="utf-8")), run_path.parent)
    assert any("SHA-256 does not match" in error for error in errors)
    with pytest.raises(ValueError, match="Cannot score"):
        score_record(run_path)
    assert main(["score", str(run_path)]) == 2
    with pytest.raises(ValueError, match="Cannot summarize invalid run records"):
        summarize([run_path])

    assert score_path.read_bytes() == previous_score
    assert run_path.read_bytes() == previous_record


def test_score_dependency_fingerprint_is_stable_for_unchanged_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = _score_fixture(tmp_path / "run" / "repeat01")
    cached = _persist_score(run_path)
    again = score_record(run_path)
    assert again["dependencies"]["fingerprint"] == cached["dependencies"]["fingerprint"]

    captured: list[dict] = []
    original = ab_eval._score_run_record

    def capture_score(*args, **kwargs):
        score = original(*args, **kwargs)
        captured.append(score)
        return score

    monkeypatch.setattr(ab_eval, "_score_run_record", capture_score)
    summarize([run_path])
    assert captured[0]["dependencies"]["fingerprint"] == cached["dependencies"]["fingerprint"]


def test_summarize_recomputes_when_human_labels_change(tmp_path: Path) -> None:
    run_path = _score_fixture(tmp_path / "run" / "repeat01")
    labels_path = run_path.parent / "labels.json"
    labels = {
        "label_source": "human",
        "status": "adjudicated",
        "case_classification": "defect",
        "known_findings": [],
        "prediction_labels": [
            {"finding_id": "finding-1", "verdict": "valid", "matches_known_findings": []}
        ],
    }
    _write_json(labels_path, labels)
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["annotation"].update({"status": "adjudicated", "human_labels": "labels.json"})
    _write_json(run_path, record)
    cached = _persist_score(run_path)
    assert cached["human_adjudication"]["precision"] == 1.0

    labels["prediction_labels"][0]["verdict"] = "false_positive"
    _write_json(labels_path, labels)
    report = summarize([run_path])

    assert report["by_condition"]["same_host_direct"]["human_precision_mean"] == 0.0
    assert json.loads((run_path.parent / "score.json").read_text(encoding="utf-8"))[
        "human_adjudication"
    ]["precision"] == 1.0


def test_summarize_recomputes_live_repository_state_instead_of_cached_verified(
    tmp_path: Path,
) -> None:
    direct = _score_fixture(
        tmp_path / "runs" / "case0001" / "same_host_direct" / "repeat01"
    )
    skill = _score_fixture(
        tmp_path / "runs" / "case0001" / "current_skill" / "repeat01",
        condition="current_skill",
    )
    _prepare_pair_summary_record(direct, condition="same_host_direct")
    _prepare_pair_summary_record(skill, condition="current_skill")
    assert json.loads((direct.parent / "score.json").read_text(encoding="utf-8"))[
        "workflow"
    ]["repository_snapshot_check"]["status"] == "verified"

    repository = Path(json.loads(direct.read_text(encoding="utf-8"))["source_paths"]["repository"])
    (repository / "untracked.txt").write_text("changed after scoring\n", encoding="utf-8")
    report = summarize([direct, skill], ["case0001"])

    pair = report["paired_cases"][0]
    assert pair["status"] == "not_comparable"
    assert pair["repository_snapshot_checks"]["same_host_direct"]["scoring"] == "mismatch"
    assert pair["repository_snapshot_checks"]["current_skill"]["scoring"] == "mismatch"


def test_summary_keeps_null_skill_provenance_as_missing_load_evidence(tmp_path: Path) -> None:
    direct = _score_fixture(
        tmp_path / "runs" / "case0001" / "same_host_direct" / "repeat01"
    )
    skill = _score_fixture(
        tmp_path / "runs" / "case0001" / "current_skill" / "repeat01",
        condition="current_skill",
    )
    _prepare_pair_summary_record(direct, condition="same_host_direct")
    _prepare_pair_summary_record(skill, condition="current_skill")
    record = json.loads(skill.read_text(encoding="utf-8"))
    record["skill_provenance"] = None
    _write_json(skill, record)

    report = summarize([direct, skill], ["case0001"])
    pair = report["paired_cases"][0]

    assert report["run_count"] == 2
    assert pair["skill_load_check"]["verified_loaded"] is None
    assert pair["skill_load_check"]["evidence_status"] == "missing_provenance"
    assert pair["status"] == "paired_inputs_and_host_identity_verified_skill_load_unverified"


def test_coverage_audit_changes_invalidate_score_dependencies(tmp_path: Path) -> None:
    run_path = _score_fixture(tmp_path / "run" / "repeat01", read_paths=["src/app.py"])
    audit_path = run_path.parent / "coverage-audit.json"
    audit = {
        "verified": True,
        "read_paths": ["src/app.py"],
        "uncovered_paths": [],
        "context_omissions": [],
        "review_status": "completed",
        "scope_verified": True,
    }
    _write_json(audit_path, audit)
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["artifacts"]["coverage_audit"] = "coverage-audit.json"
    record["execution"]["coverage_audit"] = {
        "status": "verified",
        "artifact": "coverage-audit.json",
        "source": None,
        "auditor": None,
        "read_paths": ["src/app.py"],
        "uncovered_paths": [],
        "context_omissions": [],
        "review_status": "completed",
        "scope_verified": True,
        "notes": [],
    }
    _write_json(run_path, record)
    before = score_record(run_path)
    assert before["workflow"]["coverage_complete"] is True

    audit["uncovered_paths"] = ["src/app.py"]
    _write_json(audit_path, audit)
    after = score_record(run_path)

    assert after["workflow"]["coverage_complete"] is False
    assert after["dependencies"]["fingerprint"] != before["dependencies"]["fingerprint"]


def test_recheck_changes_invalidate_score_dependencies(tmp_path: Path) -> None:
    run_path = _score_fixture(
        tmp_path / "run" / "repeat01",
        recheck={"recheck_status": "completed", "findings": []},
    )
    labels_path = run_path.parent / "labels.json"
    _write_json(labels_path, {
        "label_source": "human",
        "status": "adjudicated",
        "case_classification": "defect",
        "known_findings": [],
        "prediction_labels": [
            {"finding_id": "finding-1", "verdict": "valid", "matches_known_findings": []}
        ],
        "recheck_reviews": [
            {"previous_finding_id": "finding-1", "actual_status": "persisting"}
        ],
    })
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["annotation"].update({"status": "adjudicated", "human_labels": "labels.json"})
    _write_json(run_path, record)
    before = score_record(run_path)
    assert before["human_adjudication"]["wrong_resolved_count"] == 0

    _write_json(run_path.parent / "recheck.json", {
        "recheck_status": "completed",
        "findings": [{"previous_finding_id": "finding-1", "status": "resolved"}],
    })
    after = score_record(run_path)

    assert after["human_adjudication"]["wrong_resolved_count"] == 1
    assert after["dependencies"]["fingerprint"] != before["dependencies"]["fingerprint"]


def test_scorer_version_change_invalidates_dependency_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = _score_fixture(tmp_path / "run" / "repeat01")
    cached = _persist_score(run_path)
    monkeypatch.setattr(ab_eval, "VERSION", "test-scorer-version")
    captured: list[dict] = []
    original = ab_eval._score_run_record

    def capture_score(*args, **kwargs):
        score = original(*args, **kwargs)
        captured.append(score)
        return score

    monkeypatch.setattr(ab_eval, "_score_run_record", capture_score)
    summarize([run_path])
    changed = captured[0]

    assert changed["scorer"]["version"] == "test-scorer-version"
    assert changed["scorer"]["implementation_sha256"] == cached["scorer"]["implementation_sha256"]
    assert changed["dependencies"]["fingerprint"] != cached["dependencies"]["fingerprint"]


@pytest.mark.parametrize("mutation", ["unchanged", "raw", "input", "both"])
def test_build_runs_refuses_to_rebuild_recorded_attempt_without_mutation(
    tmp_path: Path, mutation: str
) -> None:
    index_path, runs_root, case, run_dir = _prepare_build_runs(tmp_path)
    run_path = _run_builds(index_path, runs_root)[0]
    score_path = run_dir / "score.json"
    input_copy = run_dir / "artifacts" / "reviewer-input.json"
    preserved = {
        run_path: run_path.read_bytes(),
        score_path: score_path.read_bytes(),
        input_copy: input_copy.read_bytes(),
    }

    if mutation in {"raw", "both"}:
        host_path = run_dir / "host-output.json"
        host_path.write_bytes(host_path.read_bytes() + b"\n")
    if mutation in {"input", "both"}:
        input_path = Path(case["reviewer_input"])
        input_path.write_bytes(input_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="Attempt is already recorded"):
        _run_builds(index_path, runs_root)

    assert {path: path.read_bytes() for path in preserved} == preserved


def test_build_runs_preflights_every_attempt_before_creating_any_record(
    tmp_path: Path,
) -> None:
    index_path, runs_root, case, first_run_dir = _prepare_build_runs(tmp_path)
    second_run_dir = runs_root / case["case_id"] / "current_skill" / "repeat01"
    second_run_dir.mkdir(parents=True)
    _write_json(second_run_dir / "host-output.json", {"run_status": "completed", "findings": []})
    existing_record = second_run_dir / "run.json"
    existing_score = second_run_dir / "score.json"
    existing_copy = second_run_dir / "artifacts" / "reviewer-input.json"
    _write_json(existing_record, {"preserved": "run"})
    _write_json(existing_score, {"preserved": "score"})
    existing_copy.parent.mkdir(parents=True)
    existing_copy.write_bytes(b"preserved input copy\n")
    preserved = {
        existing_record: existing_record.read_bytes(),
        existing_score: existing_score.read_bytes(),
        existing_copy: existing_copy.read_bytes(),
    }

    with pytest.raises(ValueError, match="Attempt is already recorded"):
        _run_builds(index_path, runs_root)

    assert not (first_run_dir / "run.json").exists()
    assert not (first_run_dir / "score.json").exists()
    assert not (first_run_dir / "artifacts" / "reviewer-input.json").exists()
    assert {path: path.read_bytes() for path in preserved} == preserved


def test_build_run_record_refuses_existing_attempt_before_copying_input(
    tmp_path: Path,
) -> None:
    index, case, run_dir = _build_case(tmp_path)
    _write_json(run_dir / "host-output.json", {"run_status": "completed", "findings": []})
    first_record = _build_record(index, case, run_dir)
    run_path = run_dir / "run.json"
    score_path = run_dir / "score.json"
    _write_json(run_path, first_record)
    _write_json(score_path, {"preserved": "score"})
    input_copy = run_dir / "artifacts" / "reviewer-input.json"
    preserved = {
        run_path: run_path.read_bytes(),
        score_path: score_path.read_bytes(),
        input_copy: input_copy.read_bytes(),
    }
    input_path = Path(case["reviewer_input"])
    input_path.write_bytes(input_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="Attempt is already recorded"):
        _build_record(index, case, run_dir)

    assert {path: path.read_bytes() for path in preserved} == preserved


def test_build_cli_and_summary_share_normalized_score_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, case, run_dir = _build_case(tmp_path)
    findings = [{
        "finding_id": "finding-1",
        "root_cause": "A concrete root cause.",
        "trigger": "A concrete trigger.",
        "impact": "A concrete impact.",
        "introduced_by_change": True,
        "evidence": [],
    }]
    _write_json(run_dir / "host-output.json", {
        "condition": "same_host_direct",
        "run_status": "completed",
        "review_status": "completed",
        "scope": {"base": case["base"], "head": case["head"], "paths": ["src/app.py"]},
        "read_paths": ["src/app.py"],
        "uncovered_paths": [],
        "context_omissions": [],
        "findings": findings,
        "host": {"name": "same-host", "version": "1", "model": "model"},
        "model_usage": {},
    })
    _write_json(run_dir / "validated-result.json", {"findings": findings})
    index_path = tmp_path / "index.json"
    index["cases"] = [case]
    _write_json(index_path, index)
    (tmp_path / "runs").mkdir(exist_ok=True)

    paths = build_runs(
        index_path,
        tmp_path / "runs",
        skill_root=tmp_path / "missing-skill",
        planned_replicates=1,
        order_seed="test-seed",
        skill_label="test-skill",
        skill_revision=None,
        skill_bundle_sha256=None,
        skill_bundle_path=None,
    )
    run_path = paths[0]
    built_score = json.loads((run_dir / "score.json").read_text(encoding="utf-8"))
    assert main(["score", str(run_path)]) == 0
    cli_score = json.loads((run_dir / "score.json").read_text(encoding="utf-8"))
    assert cli_score == built_score

    captured: list[dict] = []
    original = ab_eval._score_run_record

    def capture_score(*args, **kwargs):
        score = original(*args, **kwargs)
        captured.append(score)
        return score

    monkeypatch.setattr(ab_eval, "_score_run_record", capture_score)
    report = summarize([run_path], [case["case_id"]])

    assert report["run_count"] == 1
    assert captured[0] == cli_score
    assert captured[0]["dependencies"]["fingerprint"] == built_score["dependencies"]["fingerprint"]


def test_legacy_run_without_original_immutable_digests_cannot_be_rescored(
    tmp_path: Path,
) -> None:
    run_path = _score_fixture(tmp_path / "run" / "repeat01")
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["artifacts"].pop("validated_result_sha256")
    record["input"].pop("review_manifest_sha256")
    record["scoring"] = {
        "status": "completed",
        "script": "eval/ab_eval.py",
        "version": "0.2.0",
        "output": "score.json",
    }
    _write_json(run_path, record)
    _write_json(run_path.parent / "score.json", {"schema_version": 1, "legacy": True})
    run_bytes = run_path.read_bytes()
    score_bytes = (run_path.parent / "score.json").read_bytes()

    errors = validate_record(record, run_path.parent)
    assert any("artifacts.validated_result_sha256 is required" in error for error in errors)
    assert any("input.review_manifest_sha256 is required" in error for error in errors)
    with pytest.raises(ValueError, match="Cannot score"):
        score_record(run_path)
    with pytest.raises(ValueError, match="Cannot summarize invalid run records"):
        summarize([run_path])
    assert main(["score", str(run_path)]) == 2

    assert run_path.read_bytes() == run_bytes
    assert (run_path.parent / "score.json").read_bytes() == score_bytes


def test_legacy_run_with_original_immutable_digests_can_be_rescored(
    tmp_path: Path,
) -> None:
    run_path = _score_fixture(tmp_path / "run" / "repeat01")
    record = json.loads(run_path.read_text(encoding="utf-8"))
    original_digests = {
        (group, name): record[group][name]
        for group, name in (
            ("artifacts", "raw_host_output_sha256"),
            ("artifacts", "validated_result_sha256"),
            ("input", "review_manifest_sha256"),
        )
    }
    record["scoring"] = {
        "status": "completed",
        "script": "eval/ab_eval.py",
        "version": "0.2.0",
        "output": "score.json",
    }
    _write_json(run_path, record)
    _write_json(run_path.parent / "score.json", {"schema_version": 1, "legacy": True})

    assert validate_record(record, run_path.parent) == []
    legacy_rescore = score_record(run_path)
    assert len(legacy_rescore["dependencies"]["fingerprint"]) == 64
    assert summarize([run_path])["run_count"] == 1
    assert main(["score", str(run_path)]) == 0

    upgraded = json.loads(run_path.read_text(encoding="utf-8"))
    for (group, name), digest in original_digests.items():
        assert upgraded[group][name] == digest
    assert upgraded["scoring"]["dependency_manifest_version"] == 1
    assert upgraded["scoring"]["dependency_fingerprint"] == legacy_rescore["dependencies"]["fingerprint"]
    assert validate_record(upgraded, run_path.parent) == []
