#!/usr/bin/env python3
"""Build and score auditable, host-neutral A/B review records without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__:
    from .runtime_evidence import RuntimeEvidenceError, verify_runtime_evidence
else:
    from runtime_evidence import RuntimeEvidenceError, verify_runtime_evidence


VERSION = "0.2.0"
CONDITIONS = ("same_host_direct", "current_skill")
CONDITION_ALIASES = {"A": "same_host_direct", "B": "current_skill"}
RUN_STATUSES = {"not_run", "completed", "failed", "incomplete"}
REVIEW_STATUSES = {"completed", "partial", "uncovered", "failed"}
HUMAN_VERDICTS = {"valid", "false_positive", "duplicate", "unresolved"}
ROOT = Path(__file__).resolve().parents[1]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _scorer_identity() -> dict[str, Any]:
    implementation_files = {
        "eval/ab_eval.py": Path(__file__).resolve(),
        "eval/runtime_evidence.py": Path(__file__).with_name("runtime_evidence.py").resolve(),
        "eval/runtime_trace.py": Path(__file__).with_name("runtime_trace.py").resolve(),
        "eval/schemas/run-record.schema.json": ROOT / "eval" / "schemas" / "run-record.schema.json",
    }
    file_digests = {
        name: _file_sha256(path) for name, path in implementation_files.items()
    }
    return {
        "name": "eval.ab_eval",
        "version": VERSION,
        "implementation_sha256": _sha256(_canonical_bytes(file_digests)),
        "implementation_files": file_digests,
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _relative(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()


def _resolve_reference(run_dir: Path, reference: str | None) -> Path | None:
    if reference is None:
        return None
    path = Path(reference)
    return path if path.is_absolute() else (run_dir / path).resolve()


def _load_reference(run_dir: Path, reference: str | None) -> Any | None:
    path = _resolve_reference(run_dir, reference)
    if path is None or not path.is_file():
        return None
    return _read_json(path)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _elapsed_seconds(output: dict[str, Any]) -> float | None:
    supplied = output.get("duration_seconds")
    if isinstance(supplied, (int, float)) and not isinstance(supplied, bool) and supplied >= 0:
        return float(supplied)
    start = _parse_time(output.get("started_at"))
    finish = _parse_time(output.get("finished_at"))
    if start is None or finish is None:
        return None
    elapsed = (finish - start).total_seconds()
    return elapsed if elapsed >= 0 else None


def _fingerprint_snapshot(case: dict[str, Any], reviewer_input: dict[str, Any]) -> str | None:
    snapshot = reviewer_input.get("snapshot") or case.get("snapshot")
    return _sha256(_canonical_bytes(snapshot)) if isinstance(snapshot, dict) else None


def _fingerprint_scope_values(scope: Any, base: Any, head: Any, paths: Any) -> str:
    payload = {
        "scope": scope,
        "base": base,
        "head": head,
        "paths": sorted(paths) if isinstance(paths, list) else [],
    }
    return _sha256(_canonical_bytes(payload))


def _fingerprint_scope(case: dict[str, Any], reviewer_input: dict[str, Any]) -> str:
    return _fingerprint_scope_values(
        reviewer_input.get("scope", case.get("scope")),
        reviewer_input.get("base", case.get("base")),
        reviewer_input.get("head", case.get("head")),
        reviewer_input.get("scope_paths", case.get("scope_paths", [])),
    )


def _review_input_fingerprint_check(record: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    input_record = record.get("input", {})
    ref = input_record.get("review_manifest")
    reviewer_input = _load_reference(run_dir, ref)
    expected = record.get("repository_snapshot", {})
    if not isinstance(reviewer_input, dict):
        return {
            "status": "unavailable",
            "snapshot_fingerprint_matches": None,
            "review_scope_fingerprint_matches": None,
            "repository_identity_matches": None,
        }
    snapshot = reviewer_input.get("snapshot")
    expected_snapshot = expected.get("snapshot")
    snapshot_fingerprint = (
        _sha256(_canonical_bytes(snapshot)) if isinstance(snapshot, dict) else None
    )
    snapshot_matches = (
        isinstance(snapshot, dict)
        and snapshot == expected_snapshot
        and snapshot_fingerprint == input_record.get("snapshot_fingerprint")
    )
    scope_fingerprint = _fingerprint_scope({}, reviewer_input)
    scope_matches = scope_fingerprint == input_record.get("review_scope_fingerprint")
    expected_paths = input_record.get("scope_paths")
    repository_identity_matches = (
        reviewer_input.get("base") == expected.get("base_revision")
        and reviewer_input.get("head") == expected.get("head_revision")
        and isinstance(expected_paths, list)
        and isinstance(reviewer_input.get("scope_paths"), list)
        and sorted(reviewer_input.get("scope_paths", [])) == sorted(expected_paths)
    )
    values = [snapshot_matches, scope_matches, repository_identity_matches]
    status = "mismatch" if any(value is False for value in values) else (
        "verified" if all(value is True for value in values) else "unavailable"
    )
    return {
        "status": status,
        "snapshot_fingerprint_matches": snapshot_matches,
        "review_scope_fingerprint_matches": scope_matches,
        "repository_identity_matches": repository_identity_matches,
    }


def _skill_inventory(
    skill_root: Path,
    run_dir: Path,
    bundle_source: Path | None,
) -> tuple[dict[str, Any], str, str, str]:
    files: list[dict[str, Any]] = []
    for path in sorted(skill_root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(skill_root).as_posix()
        content = path.read_bytes()
        files.append({"path": relative, "sha256": _sha256(content), "bytes": len(content)})
    payload = {"schema_version": 1, "files": files}
    digest = _sha256(_canonical_bytes(payload))
    payload["inventory_sha256"] = digest
    inventory_path = run_dir / "skill-inventory-record.json"
    _write_json(inventory_path, payload)
    bundle_path = run_dir / "skill-bundle.zip"
    if bundle_source is not None and bundle_source.is_file() and not bundle_path.exists():
        shutil.copyfile(bundle_source, bundle_path)
    if not bundle_path.exists():
        existing_bundle = next(
            (
                path
                for path in (run_dir / "review-changes-0.2.2-dev.zip", run_dir / "skill-package.zip")
                if path.is_file()
            ),
            None,
        )
        if existing_bundle:
            bundle_path = existing_bundle
    if not bundle_path.exists():
        bundle_path = run_dir / "skill-bundle-generated.zip"
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for item in files:
                source = skill_root / item["path"]
                info = zipfile.ZipInfo(f"review-changes/{item['path']}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, source.read_bytes())
    return (
        payload,
        _relative(inventory_path, run_dir),
        _relative(bundle_path, run_dir),
        _sha256(bundle_path.read_bytes()),
    )


def _git_revision(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", value) else None


def _replicate_from_path(run_dir: Path) -> int | None:
    match = re.fullmatch(r"repeat(\d+)", run_dir.name)
    return int(match.group(1)) if match else None


def _comparison_metadata(run_dir: Path) -> tuple[int | None, int | None, str | None, str | None]:
    path_value = _replicate_from_path(run_dir)
    metadata_path = run_dir / "run-metadata.json"
    if not metadata_path.is_file():
        attempt = path_value
        return 1 if attempt is not None else None, attempt, (
            "initial" if attempt == 1 else "retry" if attempt is not None else None
        ), None
    metadata = _read_json(metadata_path)
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
        raise ValueError(f"Invalid run metadata: {metadata_path}")
    replicate = metadata.get("replicate")
    attempt = metadata.get("attempt")
    kind = metadata.get("attempt_kind")
    group_id = metadata.get("group_id")
    if not isinstance(replicate, int) or isinstance(replicate, bool) or replicate < 1:
        raise ValueError(f"run-metadata.json requires replicate >= 1: {metadata_path}")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError(f"run-metadata.json requires attempt >= 1: {metadata_path}")
    if kind not in {"initial", "retry", "independent_replicate"}:
        raise ValueError(f"run-metadata.json has invalid attempt_kind: {metadata_path}")
    if group_id is not None and (not isinstance(group_id, str) or not group_id.strip()):
        raise ValueError(f"run-metadata.json group_id must be a non-empty string: {metadata_path}")
    return replicate, attempt, kind, group_id


def _safe_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return value


def _verified_coverage_audit(run_dir: Path) -> tuple[str | None, dict[str, Any] | None]:
    path = run_dir / "coverage-audit.json"
    if not path.is_file():
        return None, None
    audit = _read_json(path)
    if not isinstance(audit, dict):
        raise ValueError(f"Coverage audit must be a JSON object: {path}")
    ref = _relative(path, run_dir)
    if audit.get("verified") is not True:
        return ref, {"status": "pending", "artifact": ref}
    read_paths = _safe_list(audit.get("read_paths"))
    uncovered_paths = _safe_list(audit.get("uncovered_paths"))
    context_omissions = _safe_list(audit.get("context_omissions"))
    review_status = audit.get("review_status")
    scope_verified = audit.get("scope_verified")
    source = audit.get("source")
    auditor = audit.get("auditor")
    trace = audit.get("trace")
    trace_valid = False
    if (
        isinstance(trace, dict)
        and set(trace) == {"path", "sha256"}
        and isinstance(trace.get("path"), str)
        and isinstance(trace.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", trace["sha256"])
    ):
        trace_path = _resolve_reference(run_dir, trace["path"])
        trace_valid = (
            trace_path is not None
            and trace_path.is_file()
            and _file_sha256(trace_path) == trace["sha256"]
        )
    if (
        read_paths is None
        or uncovered_paths is None
        or context_omissions is None
        or review_status not in REVIEW_STATUSES
        or not isinstance(scope_verified, bool)
        or not isinstance(source, str)
        or not source.strip()
        or not isinstance(auditor, str)
        or not auditor.strip()
        or not trace_valid
    ):
        return ref, {"status": "invalid", "artifact": ref}
    return ref, {
        "status": "verified",
        "artifact": ref,
        "source": source,
        "auditor": auditor,
        "trace": trace,
        "read_paths": read_paths,
        "uncovered_paths": uncovered_paths,
        "context_omissions": context_omissions,
        "review_status": review_status,
        "scope_verified": scope_verified,
        "notes": [item for item in audit.get("notes", []) if isinstance(item, str)]
        if isinstance(audit.get("notes"), list)
        else [],
    }


def _candidate_artifact(run_dir: Path, names: tuple[str, ...]) -> str | None:
    for name in names:
        path = run_dir / name
        if path.is_file():
            return _relative(path, run_dir)
    return None


def _resolve_annotation_reference(
    reference: str,
    operator_root: Path,
    annotation_path: Path,
) -> Path:
    path = Path(reference)
    if path.is_absolute():
        return path
    candidates = [
        (annotation_path.parent / path).resolve(),
        (operator_root / path).resolve(),
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def _repository_snapshot_check(
    repository: str | Path | None,
    base: str | None,
    head: str | None,
    snapshot: Any,
    expected_fingerprint: str | None,
    *,
    case_input_matches: bool | None = None,
) -> dict[str, Any]:
    expected = snapshot if isinstance(snapshot, dict) else {}
    observed: dict[str, Any] = {
        "base_revision": None,
        "head_revision": None,
        "base_tree": None,
        "head_tree": None,
        "full_diff_sha256": None,
    }
    checks: dict[str, bool | None] = {
        "base_revision_matches": None,
        "head_matches": None,
        "base_tree_matches": None,
        "head_tree_matches": None,
        "full_diff_matches": None,
        "snapshot_fingerprint_matches": None,
        "case_and_reviewer_input_match": case_input_matches,
    }
    worktree_status = "unavailable"
    repo = Path(repository) if repository is not None else None
    if repo is not None and repo.is_dir() and base and head:
        def git(*args: str) -> bytes | None:
            try:
                result = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    check=False,
                    capture_output=True,
                )
            except OSError:
                return None
            return result.stdout if result.returncode == 0 else None

        base_revision_bytes = git("rev-parse", f"{base}^{{commit}}")
        head_bytes = git("rev-parse", "HEAD")
        base_tree_bytes = git("rev-parse", f"{base}^{{tree}}")
        head_tree_bytes = git("rev-parse", f"{head}^{{tree}}")
        diff_bytes = git("diff", "--binary", base, head)
        status_bytes = git("status", "--porcelain=v1", "--untracked-files=all")
        observed.update({
            "base_revision": base_revision_bytes.decode("ascii").strip()
            if base_revision_bytes is not None
            else None,
            "head_revision": head_bytes.decode("ascii").strip() if head_bytes is not None else None,
            "base_tree": base_tree_bytes.decode("ascii").strip() if base_tree_bytes is not None else None,
            "head_tree": head_tree_bytes.decode("ascii").strip() if head_tree_bytes is not None else None,
            "full_diff_sha256": _sha256(diff_bytes) if diff_bytes is not None else None,
        })
        checks.update({
            "base_revision_matches": observed["base_revision"] == base
            if base_revision_bytes is not None
            else None,
            "head_matches": observed["head_revision"] == head if head_bytes is not None else None,
            "base_tree_matches": observed["base_tree"] == expected.get("base_tree")
            if base_tree_bytes is not None and isinstance(expected.get("base_tree"), str)
            else None,
            "head_tree_matches": observed["head_tree"] == expected.get("head_tree")
            if head_tree_bytes is not None and isinstance(expected.get("head_tree"), str)
            else None,
            "full_diff_matches": observed["full_diff_sha256"] == expected.get("scoped_diff_sha256")
            if diff_bytes is not None and isinstance(expected.get("scoped_diff_sha256"), str)
            else None,
            "snapshot_fingerprint_matches": _sha256(_canonical_bytes(expected)) == expected_fingerprint
            if expected and isinstance(expected_fingerprint, str)
            else None,
        })
        worktree_status = (
            "clean" if status_bytes == b"" else "dirty" if status_bytes is not None else "unavailable"
        )

    check_values = list(checks.values())
    if any(value is False for value in check_values) or worktree_status == "dirty":
        status = "mismatch"
    elif all(value is True for value in check_values) and worktree_status == "clean":
        status = "verified"
    else:
        status = "unverified"
    return {
        "status": status,
        "checks": checks,
        "expected": {
            "base_revision": base,
            "head_revision": head,
            "snapshot": expected or None,
            "snapshot_fingerprint": expected_fingerprint,
        },
        "observed": observed,
        "worktree_status": worktree_status,
        "limitation": "Build/scoring snapshot checks cannot establish that the repository remained unchanged during review.",
    }


def _protocol_incidents(operator_root: Path, run_dir: Path, host_output: Path) -> list[dict[str, Any]]:
    incidents_root = operator_root / "incidents"
    if not incidents_root.is_dir():
        return []
    raw_path = host_output.resolve()
    raw_digest = _sha256(host_output.read_bytes()) if host_output.is_file() else None
    matched: list[dict[str, Any]] = []
    for incident_path in sorted(incidents_root.rglob("*.json")):
        try:
            incident = _read_json(incident_path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(incident, dict):
            continue
        incident_raw = incident.get("raw_host_output")
        if not isinstance(incident_raw, str):
            continue
        referenced = Path(incident_raw)
        if not referenced.is_absolute():
            referenced = (operator_root / referenced).resolve()
        if referenced.resolve() != raw_path:
            continue
        event = incident.get("event")
        if not (
            incident.get("protocol_violation") is True
            or isinstance(event, str) and "protocol_violation" in event
        ):
            continue
        expected_digest = incident.get("raw_host_output_sha256")
        matched_incident = {
            "artifact": _relative(incident_path, run_dir),
            "sha256": _sha256(incident_path.read_bytes()),
            "incident_id": incident.get("incident_id") if isinstance(incident.get("incident_id"), str) else None,
            "event": event if isinstance(event, str) else None,
            "raw_host_output_sha256_matches": expected_digest == raw_digest
            if isinstance(expected_digest, str)
            else None,
        }
        restoration = incident.get("restoration_attribution")
        if isinstance(restoration, dict):
            actor = restoration.get("actor")
            role = restoration.get("role")
            phase = restoration.get("phase")
            if all(isinstance(value, str) and value for value in (actor, role, phase)):
                matched_incident["restoration_attribution"] = {
                    "actor": actor,
                    "role": role,
                    "phase": phase,
                }
        matched.append(matched_incident)
    return matched


def _annotation_state(case: dict[str, Any], operator_root: Path, run_dir: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_path = Path(case.get("annotation", ""))
    if not raw_path.is_absolute():
        raw_path = (operator_root / raw_path).resolve()
    if not raw_path.is_file():
        return (
            {"status": "pending", "oracle": None, "human_labels": None, "provisional_labels": None},
            None,
        )
    annotation = _read_json(raw_path)
    if not isinstance(annotation, dict):
        raise ValueError(f"Operator annotation must be a JSON object: {raw_path}")
    human_labels_ref = case.get("human_labels")
    if not isinstance(human_labels_ref, str):
        human_labels_ref = annotation.get("human_labels")
    resolved_human_labels_path = (
        _resolve_annotation_reference(human_labels_ref, operator_root, raw_path)
        if isinstance(human_labels_ref, str)
        else None
    )
    human_labels_data = (
        _read_json(resolved_human_labels_path)
        if resolved_human_labels_path is not None and resolved_human_labels_path.is_file()
        else None
    )
    reported_statuses = {
        case.get("human_label_status"),
        annotation.get("human_label_status"),
    }
    if isinstance(human_labels_data, dict):
        reported_statuses.add(human_labels_data.get("status"))
    if (
        isinstance(human_labels_data, dict)
        and human_labels_data.get("label_source") == "human"
        and human_labels_data.get("status") == "adjudicated"
    ):
        status = "adjudicated"
    elif "reviewed" in reported_statuses or "adjudicated" in reported_statuses:
        status = "reviewed"
    elif "not_started" in reported_statuses:
        status = "not_started"
    else:
        status = "pending"
    relative_path = _relative(raw_path, run_dir)
    return (
        {
            "status": status,
            "oracle": relative_path,
            "human_labels": _relative(resolved_human_labels_path, run_dir)
            if resolved_human_labels_path is not None and resolved_human_labels_path.is_file()
            else None,
            "provisional_labels": relative_path,
            "provisional_source_sha256": _sha256(raw_path.read_bytes()),
        },
        annotation,
    )


def _scope_verified(case: dict[str, Any], reviewer_input: dict[str, Any], output: dict[str, Any]) -> bool | None:
    actual = output.get("scope")
    if not isinstance(actual, dict):
        return None
    expected_paths = sorted(reviewer_input.get("scope_paths", case.get("scope_paths", [])))
    actual_paths = actual.get("paths")
    if not isinstance(actual_paths, list) or any(not isinstance(path, str) for path in actual_paths):
        return None
    return (
        actual.get("base") == reviewer_input.get("base", case.get("base"))
        and actual.get("head") == reviewer_input.get("head", case.get("head"))
        and sorted(actual_paths) == expected_paths
    )


def _usage(output: dict[str, Any]) -> dict[str, Any]:
    usage = output.get("model_usage")
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": usage.get("input_tokens")
        if isinstance(usage.get("input_tokens"), int) and not isinstance(usage.get("input_tokens"), bool) and usage.get("input_tokens") >= 0
        else None,
        "output_tokens": usage.get("output_tokens")
        if isinstance(usage.get("output_tokens"), int) and not isinstance(usage.get("output_tokens"), bool) and usage.get("output_tokens") >= 0
        else None,
        "cost": usage.get("cost")
        if isinstance(usage.get("cost"), (int, float)) and not isinstance(usage.get("cost"), bool) and usage.get("cost") >= 0
        else None,
        "currency": usage.get("currency") if isinstance(usage.get("currency"), str) else None,
    }


def _ensure_unrecorded_attempt(run_dir: Path) -> None:
    record_path = run_dir / "run.json"
    if record_path.exists() or record_path.is_symlink():
        raise ValueError(
            f"Attempt is already recorded: {record_path}. Use `score` to rescore complete, "
            "digest-bound data; use a new attempt for a new review. Historical attempts "
            "without complete bindings are descriptive replay only."
        )


def build_run_record(
    index: dict[str, Any],
    case: dict[str, Any],
    condition: str,
    run_dir: Path,
    *,
    blind_root: Path,
    operator_root: Path,
    skill_root: Path,
    run_order: int | None,
    concurrent: bool | None,
    planned_replicates: int | None,
    order_seed: str | None,
    skill_label: str,
    skill_revision: str | None,
    skill_bundle_sha256: str | None,
    skill_bundle_path: Path | None,
) -> dict[str, Any]:
    _ensure_unrecorded_attempt(run_dir)
    host_path = run_dir / "host-output.json"
    output_bytes = host_path.read_bytes()
    output = json.loads(output_bytes.decode("utf-8"))
    if not isinstance(output, dict):
        raise ValueError(f"Host output must be a JSON object: {host_path}")
    if output.get("case_id") not in {None, case["case_id"]}:
        raise ValueError(f"Host output case_id does not match {case['case_id']}: {host_path}")
    raw_condition = output.get("condition")
    normalization_events: list[dict[str, str]] = []
    if raw_condition is not None and raw_condition != condition:
        normalized_condition = (
            CONDITION_ALIASES.get(raw_condition) if isinstance(raw_condition, str) else None
        )
        if normalized_condition != condition:
            raise ValueError(f"Host output condition does not match {condition}: {host_path}")
        normalization_events.append(
            {
                "field": "condition",
                "raw_value": raw_condition,
                "normalized_value": condition,
                "rule": "A/B alias mapped to the run-directory condition",
            }
        )

    reviewer_input_path = Path(case["reviewer_input"])
    reviewer_input = _read_json(reviewer_input_path)
    copied_input = run_dir / "artifacts" / "reviewer-input.json"
    copied_input.parent.mkdir(parents=True, exist_ok=True)
    copied_input.write_bytes(reviewer_input_path.read_bytes())

    raw_status = output.get("run_status")
    run_status = raw_status if raw_status in RUN_STATUSES else "incomplete"
    review_status = output.get("review_status")
    if review_status not in REVIEW_STATUSES:
        review_status = None
    uncovered = _safe_list(output.get("uncovered_paths"))
    omissions = _safe_list(output.get("context_omissions"))
    read_paths = _safe_list(output.get("read_paths"))

    annotation_record, _annotation = _annotation_state(case, operator_root, run_dir)
    skill_provenance: dict[str, Any] | None = None
    skill_inventory_path = None
    skill_bundle_artifact_path = None
    if condition == "current_skill" and skill_root.is_dir():
        inventory, skill_inventory_path, local_bundle_path, generated_bundle_hash = _skill_inventory(
            skill_root, run_dir, skill_bundle_path
        )
        revision = skill_revision or _git_revision(skill_root)
        actual_bundle_path = _resolve_reference(run_dir, local_bundle_path)
        actual_bundle_hash = _sha256(actual_bundle_path.read_bytes()) if actual_bundle_path and actual_bundle_path.is_file() else None
        if skill_bundle_sha256 and actual_bundle_hash != skill_bundle_sha256.lower():
            raise ValueError("Pinned Skill bundle digest does not match the preserved bundle file")
        expected_bundle_hash = actual_bundle_hash or generated_bundle_hash
        skill_bundle_artifact_path = local_bundle_path if actual_bundle_hash == expected_bundle_hash else None
        host_bundle_hash = output.get("skill_bundle_sha256")
        if not isinstance(host_bundle_hash, str):
            host_provenance = output.get("skill_provenance")
            host_bundle_hash = host_provenance.get("bundle_sha256") if isinstance(host_provenance, dict) else None
        host_digest_status = "missing"
        if isinstance(host_bundle_hash, str) and host_bundle_hash:
            if re.fullmatch(r"[0-9a-fA-F]{64}", host_bundle_hash):
                host_bundle_hash = host_bundle_hash.lower()
                host_digest_status = "valid"
            else:
                host_bundle_hash = None
                host_digest_status = "invalid"
        skill_provenance = {
            "label": skill_label,
            "source_revision": revision,
            "bundle_sha256": expected_bundle_hash,
            "provided_bundle_sha256": skill_bundle_sha256.lower() if skill_bundle_sha256 else None,
            "inventory_sha256": inventory["inventory_sha256"],
            "host_reported_bundle_sha256": host_bundle_hash,
            "host_reported_bundle_digest_status": host_digest_status,
            "verified_loaded": None,
        }
    else:
        skill_bundle_artifact_path = None

    repository_path = case.get("repositories", {}).get(condition)
    expected_snapshot = case.get("snapshot")
    reviewer_snapshot = reviewer_input.get("snapshot")
    case_input_matches = None
    case_paths = case.get("scope_paths")
    reviewer_paths = reviewer_input.get("scope_paths")
    if (
        isinstance(expected_snapshot, dict)
        and isinstance(reviewer_snapshot, dict)
        and isinstance(case_paths, list)
        and isinstance(reviewer_paths, list)
    ):
        case_input_matches = (
            expected_snapshot == reviewer_snapshot
            and case.get("base") == reviewer_input.get("base")
            and case.get("head") == reviewer_input.get("head")
            and case.get("scope") == reviewer_input.get("scope")
            and sorted(case_paths) == sorted(reviewer_paths)
    )
    if not isinstance(expected_snapshot, dict) and isinstance(reviewer_snapshot, dict):
        expected_snapshot = reviewer_snapshot
    snapshot_fingerprint = _fingerprint_snapshot(case, reviewer_input)
    repository_build_check = _repository_snapshot_check(
        repository_path,
        case.get("base", reviewer_input.get("base")),
        case.get("head", reviewer_input.get("head")),
        expected_snapshot,
        snapshot_fingerprint,
        case_input_matches=case_input_matches,
    )
    protocol_incidents = _protocol_incidents(operator_root, run_dir, host_path)
    environment = output.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    host = output.get("host") if isinstance(output.get("host"), dict) else {}
    comparison_group = f"{index.get('benchmark_id', '')}:{case['case_id']}"
    case_group = case.get("case_group_id", case["case_id"])
    replicate, attempt, attempt_kind, metadata_group_id = _comparison_metadata(run_dir)

    artifacts = {
        "raw_transcript": None,
        "raw_transcript_sha256": None,
        "raw_host_output": "host-output.json",
        "raw_host_output_sha256": _sha256(output_bytes),
        "validated_result": _candidate_artifact(run_dir, ("validated-result.json", "validated_result.json")),
        "validated_result_sha256": None,
        "markdown_report": _candidate_artifact(run_dir, ("report.md", "review.md")),
        "recheck_json": _candidate_artifact(run_dir, ("recheck.json", "recheck-result.json")),
        "recheck_markdown": _candidate_artifact(run_dir, ("recheck.md", "recheck-report.md")),
        "review_prompt": _candidate_artifact(run_dir, ("prompt.txt", "review-prompt.txt")),
        "skill_inventory": skill_inventory_path,
        "skill_bundle": skill_bundle_artifact_path,
        "run_metadata": _candidate_artifact(run_dir, ("run-metadata.json",)),
        "coverage_audit": _candidate_artifact(run_dir, ("coverage-audit.json",)),
        "runtime_evidence": _candidate_artifact(run_dir, ("runtime-evidence.json",)),
    }
    if artifacts["validated_result"]:
        validated_path = _resolve_reference(run_dir, artifacts["validated_result"])
        if validated_path is not None and validated_path.is_file():
            artifacts["validated_result_sha256"] = _file_sha256(validated_path)
    run_metadata_path = artifacts["run_metadata"]
    host_interventions = output.get("manual_interventions")
    host_interventions = (
        host_interventions
        if isinstance(host_interventions, int) and not isinstance(host_interventions, bool) and host_interventions >= 0
        else None
    )
    operator_event_path = _candidate_artifact(run_dir, ("operator-events.json", "operator-intervention.json"))
    operator_interventions = None
    operator_event_count = None
    if operator_event_path:
        event_data = _read_json(run_dir / operator_event_path)
        if isinstance(event_data, dict) and event_data.get("verified") is True:
            events = event_data.get("events")
            if (
                isinstance(events, list)
                and all(
                    isinstance(event, dict)
                    and event.get("actor") in {"agent", "user", "system"}
                    and isinstance(event.get("kind"), str)
                    and isinstance(event.get("count"), int)
                    and not isinstance(event.get("count"), bool)
                    and event["count"] >= 0
                    for event in events
                )
            ):
                operator_event_count = sum(event["count"] for event in events)
                operator_interventions = sum(
                    event["count"] for event in events if event["actor"] == "user"
                )
    effective_interventions = host_interventions if host_interventions is not None else (
        operator_interventions if operator_interventions and operator_interventions > 0 else None
    )
    _, coverage_audit = _verified_coverage_audit(run_dir)
    record = {
        "schema_version": 1,
        "benchmark_id": index["benchmark_id"],
        "case_id": case["case_id"],
        "case_origin": case.get("case_origin", "real_repository"),
        "condition": condition,
        "run_status": run_status,
        "host": {
            "name": host.get("name") if isinstance(host.get("name"), str) else None,
            "version": host.get("version") if isinstance(host.get("version"), str) else None,
            "model": host.get("model") if isinstance(host.get("model"), str) else None,
            "configuration_fingerprint": host.get("configuration_fingerprint")
            if isinstance(host.get("configuration_fingerprint"), str)
            else None,
        },
        "environment": {
            "os": environment.get("os") if isinstance(environment.get("os"), str) else None,
            "architecture": environment.get("architecture")
            if isinstance(environment.get("architecture"), str)
            else None,
        },
        "input": {
            "repository_id": case.get("source_repository"),
            "review_manifest": _relative(copied_input, run_dir),
            "review_manifest_sha256": _file_sha256(copied_input),
            "snapshot_fingerprint": _fingerprint_snapshot(case, reviewer_input),
            "review_scope_fingerprint": _fingerprint_scope(case, reviewer_input),
            "scope_paths": sorted(reviewer_input.get("scope_paths", case.get("scope_paths", []))),
            "prior_result": None,
            "prior_result_sha256": None,
            "current_manifest": artifacts["validated_result"],
            "current_manifest_sha256": artifacts["validated_result_sha256"],
        },
        "artifacts": artifacts,
        "annotation": annotation_record,
        "scoring": {"status": "not_run", "script": None, "version": None, "output": None},
        "execution": {
            "failure_reason": output.get("failure_reason") if isinstance(output.get("failure_reason"), str) else None,
            "uncovered_paths": uncovered,
            "read_paths": read_paths,
            "context_omissions": omissions,
            "review_status": review_status,
            "coverage_reported": uncovered is not None and omissions is not None and read_paths is not None,
            "scope_verified": _scope_verified(case, reviewer_input, output),
            "scope_verification_source": "host_reported" if isinstance(output.get("scope"), dict) else None,
            "coverage_audit": coverage_audit,
            "runtime_evidence": None,
            "tool_permissions": output.get("tool_permissions")
            if isinstance(output.get("tool_permissions"), str)
            else None,
            "manual_intervention_log": operator_event_path or _candidate_artifact(run_dir, ("interventions.json", "intervention-log.json")),
            "manual_interventions": effective_interventions,
            "host_reported_manual_interventions": host_interventions,
            "manual_intervention_source": "host_output" if host_interventions is not None else "verified_user_event" if operator_interventions and operator_interventions > 0 else None,
            "operator_event_count": operator_event_count,
            "duration_seconds": _elapsed_seconds(output),
            "started_at": output.get("started_at") if isinstance(output.get("started_at"), str) else None,
            "finished_at": output.get("finished_at") if isinstance(output.get("finished_at"), str) else None,
            "run_order": run_order,
            "concurrent": concurrent,
            "notes": [item for item in output.get("notes", []) if isinstance(item, str)]
            if isinstance(output.get("notes"), list)
            else [],
            "normalization_events": normalization_events,
            "reported_finding_count": len(output.get("findings")) if isinstance(output.get("findings"), list) else None,
            "protocol_incidents": protocol_incidents,
        },
        "model_usage": _usage(output),
        "comparison": {
            "group_id": metadata_group_id or f"{comparison_group}:replicate-{replicate or 'unknown'}",
            "case_group_id": case_group,
            "replicate": replicate,
            "planned_replicates": planned_replicates,
            "attempt": attempt,
            "attempt_kind": attempt_kind,
            "run_order": run_order,
            "order_seed": order_seed,
            "concurrent": concurrent,
            "metadata_artifact": run_metadata_path,
        },
        "skill_provenance": skill_provenance,
        "repository_snapshot": {
            "base_revision": case.get("base", reviewer_input.get("base")),
            "head_revision": case.get("head", reviewer_input.get("head")),
            "snapshot": expected_snapshot if isinstance(expected_snapshot, dict) else None,
            "snapshot_fingerprint": snapshot_fingerprint,
            "case_and_reviewer_input_match": case_input_matches,
            "build_check": repository_build_check,
        },
        "source_paths": {
            "repository": repository_path,
            "blind_root": str(blind_root),
            "operator_annotation": str(Path(case.get("annotation", "")).resolve()),
        },
    }
    runtime_evidence = verify_runtime_evidence(
        run_dir, artifacts["runtime_evidence"], record
    )
    record["execution"]["runtime_evidence"] = runtime_evidence
    if isinstance(skill_provenance, dict):
        skill_provenance["verified_loaded"] = runtime_evidence["verified_loaded"]
    return record


def _read_lines(repository: Path, base: str, head: str, evidence: dict[str, Any]) -> int | None:
    relative = evidence.get("path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    rel = Path(relative)
    if ".." in rel.parts or "\\" in relative or ":" in relative:
        return None
    revision = base if evidence.get("side") == "before" else head if evidence.get("side") == "after" else None
    if revision is None or not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "show", f"{revision}:{relative}"],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    try:
        return len(result.stdout.decode("utf-8").splitlines())
    except UnicodeDecodeError:
        return None


def _predictions(run_record: dict[str, Any], run_dir: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    result_ref = run_record.get("artifacts", {}).get("validated_result")
    if result_ref:
        result = _load_reference(run_dir, result_ref)
        if isinstance(result, dict) and isinstance(result.get("findings"), list):
            return result["findings"], "validated_result"
    raw = _load_reference(run_dir, run_record.get("artifacts", {}).get("raw_host_output"))
    if isinstance(raw, dict) and isinstance(raw.get("findings"), list):
        return raw["findings"], "raw_host_output"
    return None, None


def _citation_summary(
    findings: list[dict[str, Any]] | None,
    read_paths: list[str] | None,
    repository_path: str | None,
    base: str | None,
    head: str | None,
) -> dict[str, Any]:
    if findings is None or read_paths is None or repository_path is None or not base or not head:
        return {"provisional": True, "eligible": None, "citable_count": None, "uncitable_count": None}
    read = {path.replace("\\", "/") for path in read_paths}
    repo = Path(repository_path)
    citable = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        required = ("root_cause", "trigger", "impact")
        if any(not isinstance(finding.get(key), str) or not finding[key].strip() for key in required):
            continue
        introduced = finding.get("introduced_by_change")
        change_reason = finding.get("change_reason")
        if introduced not in (True, "yes", "true", "confirmed") and not (
            isinstance(change_reason, str) and change_reason.strip()
        ):
            continue
        evidence = finding.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            continue
        all_citable = True
        for item in evidence:
            if not isinstance(item, dict):
                all_citable = False
                break
            path = item.get("path", item.get("file"))
            start = item.get("line_start")
            end = item.get("line_end")
            basis = item.get("basis", item.get("description"))
            if (
                not isinstance(path, str)
                or path.replace("\\", "/") not in read
                or isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or start < 1
                or end < start
                or not isinstance(basis, str)
                or not basis.strip()
            ):
                all_citable = False
                break
            lines = _read_lines(repo, base, head, {**item, "path": path})
            if lines is None or end > lines:
                all_citable = False
                break
        if all_citable:
            citable += 1
    return {
        "provisional": True,
        "eligible": True,
        "citable_count": citable,
        "uncitable_count": len(findings) - citable,
        "definition": "Evidence paths were self-reported as read and line ranges exist in the captured repository; neither the read claim nor semantic correctness is independently established here.",
    }


def _human_metrics(
    run_record: dict[str, Any],
    run_dir: Path,
    findings: list[dict[str, Any]] | None,
    coverage_complete: bool | None,
) -> dict[str, Any]:
    nulls = {
        "precision": None,
        "known_defect_recall": None,
        "clean_case_false_alarm": None,
        "false_positive_count": None,
        "unresolved_prediction_count": None,
        "wrong_resolved_count": None,
    }
    ref = run_record.get("annotation", {}).get("human_labels")
    labels = _load_reference(run_dir, ref)
    if (
        not isinstance(labels, dict)
        or labels.get("label_source") != "human"
        or labels.get("status") != "adjudicated"
        or run_record.get("annotation", {}).get("status") != "adjudicated"
    ):
        return {"status": "pending", **nulls}

    if findings is None:
        return {"status": "adjudicated_but_predictions_unavailable", **nulls}
    case_class = labels.get("case_classification", "unknown")
    known = labels.get("known_findings")
    prediction_labels = labels.get("prediction_labels")
    expected_ids = {
        finding.get("finding_id") for finding in findings if isinstance(finding, dict)
    }
    if not isinstance(known, list) or not isinstance(prediction_labels, list):
        return {"status": "invalid_label_shape", **nulls}
    known_ids = {item.get("id") for item in known if isinstance(item, dict) and isinstance(item.get("id"), str)}
    labels_by_id = {
        item.get("finding_id"): item
        for item in prediction_labels
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    prediction_label_ids = [
        item.get("finding_id")
        for item in prediction_labels
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    ]
    finding_ids = [
        item.get("finding_id") for item in findings if isinstance(item, dict)
    ]
    valid_shape = (
        set(labels_by_id) == expected_ids
        and len(prediction_label_ids) == len(set(prediction_label_ids))
        and len(finding_ids) == len(expected_ids)
        and all(isinstance(finding_id, str) for finding_id in finding_ids)
    )
    for item in labels_by_id.values():
        matches = item.get("matches_known_findings", [])
        if item.get("verdict") not in HUMAN_VERDICTS or not isinstance(matches, list):
            valid_shape = False
            break
        if any(match not in known_ids for match in matches):
            valid_shape = False
            break
    if not valid_shape:
        return {"status": "adjudicated_labels_incomplete_or_invalid", **nulls}

    verdicts = [labels_by_id[finding_id]["verdict"] for finding_id in expected_ids]
    valid_count = verdicts.count("valid")
    false_positive_count = verdicts.count("false_positive") + verdicts.count("duplicate")
    unresolved_count = verdicts.count("unresolved")
    complete_verdicts = unresolved_count == 0
    precision = None
    if complete_verdicts and valid_count + false_positive_count > 0:
        precision = valid_count / (valid_count + false_positive_count)

    recall = None
    if coverage_complete is True and case_class in {"defect", "mixed"} and known_ids:
        matched = {
            issue_id
            for item in labels_by_id.values()
            if item.get("verdict") == "valid"
            for issue_id in item.get("matches_known_findings", [])
        }
        recall = len(known_ids & matched) / len(known_ids)

    clean_false_alarm = None
    if case_class == "clean" and complete_verdicts and coverage_complete is True:
        clean_false_alarm = false_positive_count > 0
        if valid_count > 0:
            clean_false_alarm = None

    wrong_resolved: int | None = None
    recheck_ref = run_record.get("artifacts", {}).get("recheck_json")
    recheck = _load_reference(run_dir, recheck_ref)
    if isinstance(recheck, dict) and recheck.get("recheck_status") in {"completed", "partial"}:
        resolved_ids = {
            item.get("previous_finding_id")
            for item in recheck.get("findings", [])
            if isinstance(item, dict) and item.get("status") == "resolved"
        }
        reviews = labels.get("recheck_reviews")
        if not resolved_ids:
            wrong_resolved = 0
        elif isinstance(reviews, list):
            review_map = {
                item.get("previous_finding_id"): item.get("actual_status")
                for item in reviews
                if isinstance(item, dict)
            }
            if resolved_ids <= review_map.keys() and all(
                review_map[finding_id] in {"persisting", "unverified", "resolved"}
                for finding_id in resolved_ids
            ):
                wrong_resolved = sum(review_map[finding_id] != "resolved" for finding_id in resolved_ids)

    return {
        "status": "adjudicated",
        "case_classification": case_class,
        "precision": precision,
        "known_defect_recall": recall,
        "clean_case_false_alarm": clean_false_alarm,
        "false_positive_count": false_positive_count if complete_verdicts else None,
        "unresolved_prediction_count": unresolved_count,
        "wrong_resolved_count": wrong_resolved,
        "known_findings_count": len(known_ids),
    }


def _score_record_data(
    record: dict[str, Any], run_dir: Path, case: dict[str, Any] | None = None
) -> dict[str, Any]:
    findings, finding_source = _predictions(record, run_dir)
    execution = record.get("execution", {})
    status = record.get("run_status")
    review_status = execution.get("review_status")
    uncovered = execution.get("uncovered_paths")
    omissions = execution.get("context_omissions")
    read_paths = execution.get("read_paths")
    scope_paths = record.get("input", {}).get("scope_paths")
    reported_paths_read: bool | None = None
    if isinstance(scope_paths, list) and isinstance(read_paths, list):
        normalized_read = {path.replace("\\", "/") for path in read_paths}
        reported_paths_read = all(path in normalized_read for path in scope_paths)

    reported_coverage_complete: bool | None = None
    if (
        execution.get("coverage_reported") is True
        and isinstance(execution.get("scope_verified"), bool)
        and reported_paths_read is not None
    ):
        reported_coverage_complete = (
            status == "completed"
            and review_status == "completed"
            and execution.get("scope_verified") is True
            and isinstance(uncovered, list)
            and not uncovered
            and isinstance(omissions, list)
            and not omissions
            and reported_paths_read is True
        )

    audit = execution.get("coverage_audit")
    runtime = execution.get("runtime_evidence")
    if not isinstance(runtime, dict):
        runtime = {
            "status": "missing",
            "supervisor_trace_status": "unknown",
            "verified_loaded": None,
            "protocol_violation": False,
            "common_binding_status": "incomplete",
            "common_bindings": None,
        }
    coverage_complete: bool | None = None
    if isinstance(audit, dict) and audit.get("status") == "verified":
        audited_paths = audit.get("read_paths")
        audited_uncovered = audit.get("uncovered_paths")
        audited_omissions = audit.get("context_omissions")
        audited_scope_paths_read = None
        if isinstance(scope_paths, list) and isinstance(audited_paths, list):
            normalized_audited = {path.replace("\\", "/") for path in audited_paths}
            audited_scope_paths_read = all(path in normalized_audited for path in scope_paths)
        if (
            audited_scope_paths_read is not None
            and isinstance(audited_uncovered, list)
            and isinstance(audited_omissions, list)
            and isinstance(audit.get("scope_verified"), bool)
        ):
            coverage_complete = (
                status == "completed"
                and audit.get("review_status") == "completed"
                and audit.get("scope_verified") is True
                and not audited_uncovered
                and not audited_omissions
                and audited_scope_paths_read
            )

    raw = _load_reference(run_dir, record.get("artifacts", {}).get("raw_host_output"))
    if not isinstance(raw, dict):
        raw = {}
    case = case or {}
    repository_path = record.get("source_paths", {}).get("repository")
    repository_snapshot = record.get("repository_snapshot", {})
    base = case.get("base") or repository_snapshot.get("base_revision") or raw.get("scope", {}).get("base")
    head = case.get("head") or repository_snapshot.get("head_revision") or raw.get("scope", {}).get("head")
    repository_check = _repository_snapshot_check(
        repository_path,
        base,
        head,
        repository_snapshot.get("snapshot"),
        repository_snapshot.get("snapshot_fingerprint"),
        case_input_matches=repository_snapshot.get("case_and_reviewer_input_match"),
    )
    input_fingerprint_check = _review_input_fingerprint_check(record, run_dir)
    citations = _citation_summary(findings, execution.get("read_paths"), repository_path, base, head)

    operator_path = _resolve_reference(
        run_dir, record.get("annotation", {}).get("provisional_labels")
    )
    operator_annotation = None
    if operator_path is not None and operator_path.is_file():
        operator_annotation = _read_json(operator_path)
    operator_matches: list[dict[str, Any]] = []
    if isinstance(operator_annotation, dict):
        candidate_matches = operator_annotation.get("provisional_candidate_matches")
        if isinstance(candidate_matches, list):
            operator_matches = [item for item in candidate_matches if isinstance(item, dict)]
    candidate_issue = (
        operator_annotation.get("candidate_issue")
        if isinstance(operator_annotation, dict)
        else None
    )
    finding_ids = {
        finding.get("finding_id")
        for finding in (findings or [])
        if isinstance(finding, dict) and isinstance(finding.get("finding_id"), str)
    }
    matched_ids = {
        item.get("finding_id") for item in operator_matches if item.get("finding_id") in finding_ids
    }
    pending_ids = sorted(finding_ids - matched_ids)

    human = _human_metrics(record, run_dir, findings, coverage_complete)
    usage = record.get("model_usage", {})
    score = {
        "schema_version": 1,
        "scorer": _scorer_identity(),
        "run": {
            "benchmark_id": record.get("benchmark_id"),
            "case_id": record.get("case_id"),
            "case_origin": record.get("case_origin"),
            "condition": record.get("condition"),
            "replicate": record.get("comparison", {}).get("replicate"),
            "run_order": record.get("comparison", {}).get("run_order"),
            "concurrent": record.get("comparison", {}).get("concurrent"),
            "snapshot_fingerprint": record.get("input", {}).get("snapshot_fingerprint"),
            "review_scope_fingerprint": record.get("input", {}).get("review_scope_fingerprint"),
        },
        "workflow": {
            "run_status": status,
            "review_status": review_status,
            "execution_completed": status == "completed",
            "coverage_complete": coverage_complete,
            "reported_coverage_complete_unverified": reported_coverage_complete,
            "coverage_verification_status": (
                "verified_complete" if coverage_complete is True
                else "verified_incomplete" if coverage_complete is False
                else "reported_complete_unverified" if reported_coverage_complete is True
                else "reported_incomplete_unverified" if reported_coverage_complete is False
                else "unknown"
            ),
            "coverage_audit_status": audit.get("status") if isinstance(audit, dict) else "not_audited",
            "runtime_evidence_status": runtime.get("status", "missing"),
            "supervisor_trace_status": runtime.get("supervisor_trace_status", "unknown"),
            "host_reported_completed": status == "completed",
            "host_reported_model": record.get("host", {}).get("model"),
            "service_identity_verified": None,
            "common_binding_status": runtime.get("common_binding_status", "incomplete"),
            "common_bindings": runtime.get("common_bindings"),
            "skill_load_status": runtime.get("skill_load_status", "unknown"),
            "skill_load_verified": runtime.get("verified_loaded"),
            "runtime_protocol_violation": runtime.get("protocol_violation", False),
            "scope_verified": execution.get("scope_verified"),
            "reported_scope_paths_read": reported_paths_read,
            "reported_unread_scope_paths": sorted(set(scope_paths) - set(read_paths))
            if isinstance(scope_paths, list) and isinstance(read_paths, list)
            else None,
            "uncovered_paths": uncovered if execution.get("coverage_reported") else None,
            "context_omissions": omissions if execution.get("coverage_reported") else None,
            "finding_count": len(findings) if findings is not None else None,
            "finding_source": finding_source,
            "read_paths_evidence": "host_reported_unverified" if isinstance(read_paths, list) else "not_reported",
            "duration_seconds": execution.get("duration_seconds"),
            "manual_interventions": execution.get("manual_interventions"),
            "operator_event_count": execution.get("operator_event_count"),
            "tool_runs": raw.get("tool_runs") if isinstance(raw.get("tool_runs"), list) else None,
            "failure_reason": execution.get("failure_reason"),
            "repository_snapshot_check": repository_check,
            "input_fingerprint_check": input_fingerprint_check,
            "protocol_incidents": execution.get("protocol_incidents", []),
        },
        "provisional_evidence": {
            **citations,
            "operator_candidate_issue_present": isinstance(candidate_issue, dict),
            "operator_candidate_match_count": len(matched_ids) if operator_matches else None,
            "operator_candidate_hit": bool(matched_ids) if operator_matches else None,
            "additional_findings_pending": pending_ids,
            "false_positive_count": None,
            "provisional": True,
            "label_source": "operator_candidate_evidence_not_human_verified",
        },
        "human_adjudication": human,
        "repository_integrity_limit": "Build/scoring snapshot checks cannot establish that the repository remained unchanged during review.",
        "model_usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost": usage.get("cost"),
            "currency": usage.get("currency"),
        },
    }
    return score


def _score_dependency_manifest(
    record: dict[str, Any],
    run_dir: Path,
    score: dict[str, Any],
) -> dict[str, Any]:
    file_dependencies: list[dict[str, str]] = []
    reference_fields = {
        "artifacts": (
            "raw_transcript", "raw_host_output", "validated_result", "markdown_report",
            "recheck_json", "recheck_markdown", "review_prompt", "skill_inventory",
            "skill_bundle", "run_metadata", "coverage_audit", "runtime_evidence",
        ),
        "input": ("review_manifest", "prior_result", "current_manifest"),
        "annotation": ("oracle", "human_labels", "provisional_labels"),
        "execution": ("manual_intervention_log",),
    }
    for group, names in reference_fields.items():
        values = record.get(group)
        if not isinstance(values, dict):
            continue
        for name in names:
            reference = values.get(name)
            if not isinstance(reference, str):
                continue
            path = _resolve_reference(run_dir, reference)
            if path is not None and path.is_file():
                file_dependencies.append({
                    "field": f"{group}.{name}",
                    "reference": reference,
                    "sha256": _file_sha256(path),
                })
    manifest = {
        "schema_version": 1,
        "run_record": {key: value for key, value in record.items() if key != "scoring"},
        "effective_inputs": {
            "repository": score["workflow"]["repository_snapshot_check"]["expected"],
            "snapshot_fingerprint": record.get("input", {}).get("snapshot_fingerprint"),
            "review_scope_fingerprint": record.get("input", {}).get("review_scope_fingerprint"),
            "scope_paths": record.get("input", {}).get("scope_paths"),
        },
        "files": file_dependencies,
        "repository_state": score["workflow"]["repository_snapshot_check"],
        "scorer": _scorer_identity(),
    }
    return manifest


def _score_run_record(
    record: dict[str, Any], run_dir: Path, case: dict[str, Any] | None = None
) -> dict[str, Any]:
    errors = validate_record(record, run_dir)
    if errors:
        raise ValueError(f"Cannot score run record: {'; '.join(errors)}")
    scoring_record = _scoring_record_snapshot(record)
    if isinstance(scoring_record["artifacts"].get("coverage_audit"), str):
        _coverage_ref, current_audit = _verified_coverage_audit(run_dir)
        scoring_record["execution"]["coverage_audit"] = current_audit
    runtime = verify_runtime_evidence(
        run_dir,
        scoring_record["artifacts"].get("runtime_evidence"),
        scoring_record,
    )
    scoring_record["execution"]["runtime_evidence"] = runtime
    score = _score_record_data(scoring_record, run_dir, case)
    manifest = _score_dependency_manifest(scoring_record, run_dir, score)
    score["dependencies"] = {
        "manifest": manifest,
        "fingerprint": _sha256(_canonical_bytes(manifest)),
    }
    return score


def _scoring_record_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(record)
    snapshot["artifacts"] = dict(record.get("artifacts", {}))
    snapshot["input"] = dict(record.get("input", {}))
    snapshot["execution"] = dict(record.get("execution", {}))
    return snapshot


def score_record(run_path: Path, case: dict[str, Any] | None = None) -> dict[str, Any]:
    run_path = run_path.resolve()
    run_dir = run_path.parent
    record = _read_json(run_path)
    if not isinstance(record, dict):
        raise ValueError(f"Cannot score {run_path}: run record must be a JSON object")
    try:
        return _score_run_record(record, run_dir, case)
    except ValueError as exc:
        raise ValueError(f"Cannot score {run_path}: {exc}") from exc


def validate_record(record: dict[str, Any], run_dir: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["run record must be a JSON object"]
    required = {
        "schema_version", "benchmark_id", "case_id", "case_origin", "condition",
        "run_status", "host", "environment", "input", "artifacts", "annotation",
        "scoring", "execution", "model_usage", "repository_snapshot",
    }
    missing = required - record.keys()
    if missing:
        errors.append(f"missing required run fields: {', '.join(sorted(missing))}")
        return errors
    if record.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if record.get("condition") not in {
        "same_host_direct", "current_skill", "context_selector_skill"
    }:
        errors.append("condition is not a supported review condition")
    if record.get("run_status") not in RUN_STATUSES:
        errors.append("run_status is invalid")
    nested_required = {
        "host": {"name", "version", "model"},
        "environment": {"os", "architecture"},
        "input": {"repository_id", "review_manifest", "snapshot_fingerprint"},
        "artifacts": {"raw_transcript", "raw_host_output", "validated_result", "markdown_report", "recheck_json", "recheck_markdown"},
        "annotation": {"status", "oracle", "human_labels"},
        "scoring": {"status", "script", "version", "output"},
        "execution": {"failure_reason", "uncovered_paths", "context_omissions", "manual_interventions", "duration_seconds"},
        "model_usage": {"input_tokens", "output_tokens", "cost", "currency"},
    }
    for key, keys in nested_required.items():
        value = record.get(key)
        if not isinstance(value, dict):
            errors.append(f"{key} must be an object")
            continue
        missing_keys = keys - value.keys()
        if missing_keys:
            errors.append(f"{key} missing fields: {', '.join(sorted(missing_keys))}")
    if any(not isinstance(record.get(key), dict) for key in nested_required):
        return errors
    repository_snapshot = record.get("repository_snapshot")
    if not isinstance(repository_snapshot, dict):
        errors.append("repository_snapshot must be an object")
    else:
        required_snapshot_keys = {
            "base_revision", "head_revision", "snapshot", "snapshot_fingerprint",
            "case_and_reviewer_input_match", "build_check",
        }
        missing_keys = required_snapshot_keys - repository_snapshot.keys()
        if missing_keys:
            errors.append(
                f"repository_snapshot missing fields: {', '.join(sorted(missing_keys))}"
            )
        build_check = repository_snapshot.get("build_check")
        if not isinstance(build_check, dict) or build_check.get("status") not in {
            "verified", "mismatch", "unverified",
        }:
            errors.append("repository_snapshot.build_check is invalid")
    if record.get("run_status") == "failed" and not record.get("execution", {}).get("failure_reason"):
        errors.append("failed run requires execution.failure_reason")
    if record.get("annotation", {}).get("status") not in {"not_started", "pending", "reviewed", "adjudicated"}:
        errors.append("annotation.status is invalid")
    if record.get("scoring", {}).get("status") not in {"not_run", "completed", "failed", "incomplete"}:
        errors.append("scoring.status is invalid")
    scoring = record.get("scoring", {})
    dependency_version = scoring.get("dependency_manifest_version")
    dependency_fingerprint = scoring.get("dependency_fingerprint")
    if dependency_version is not None and (dependency_version != 1 or isinstance(dependency_version, bool)):
        errors.append("scoring.dependency_manifest_version is unsupported")
    if dependency_fingerprint is not None and (
        not isinstance(dependency_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", dependency_fingerprint)
    ):
        errors.append("scoring.dependency_fingerprint must be a 64-character SHA-256 digest")
    elif dependency_version == 1 and dependency_fingerprint is None:
        errors.append("scoring.dependency_fingerprint must be a 64-character SHA-256 digest")

    refs: list[tuple[str, str | None]] = []
    for group in ("artifacts", "annotation", "input", "scoring", "execution"):
        value = record.get(group)
        if not isinstance(value, dict):
            continue
        for name, reference in value.items():
            if name in {"raw_transcript", "raw_host_output", "validated_result", "markdown_report", "recheck_json", "recheck_markdown", "review_prompt", "skill_inventory", "run_metadata", "coverage_audit", "runtime_evidence", "oracle", "human_labels", "provisional_labels", "review_manifest", "prior_result", "current_manifest", "output", "manual_intervention_log"}:
                refs.append((f"{group}.{name}", reference if isinstance(reference, str) else None))
    for label, reference in refs:
        if reference is None:
            continue
        resolved = _resolve_reference(run_dir, reference)
        if resolved is None or not resolved.is_file():
            errors.append(f"{label} references a missing file: {reference}")

    immutable_artifacts = (
        ("artifacts", "raw_transcript", "raw_transcript_sha256"),
        ("artifacts", "raw_host_output", "raw_host_output_sha256"),
        ("artifacts", "validated_result", "validated_result_sha256"),
        ("input", "review_manifest", "review_manifest_sha256"),
        ("input", "prior_result", "prior_result_sha256"),
        ("input", "current_manifest", "current_manifest_sha256"),
    )
    for group, name, digest_name in immutable_artifacts:
        values = record.get(group)
        if not isinstance(values, dict):
            continue
        reference = values.get(name)
        expected_digest = values.get(digest_name)
        if reference is None:
            if expected_digest is not None:
                errors.append(f"{group}.{digest_name} is set without {name}")
            continue
        if not isinstance(reference, str):
            errors.append(f"{group}.{name} must be a path or null")
            continue
        if expected_digest is None:
            errors.append(f"{group}.{digest_name} is required for immutable artifact {reference}")
            continue
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            errors.append(f"{group}.{digest_name} must be a 64-character SHA-256 digest")
            continue
        resolved = _resolve_reference(run_dir, reference)
        if resolved is not None and resolved.is_file() and _file_sha256(resolved) != expected_digest:
            errors.append(f"{group}.{name} SHA-256 does not match the recorded digest: {reference}")

    artifacts = record.get("artifacts", {})
    input_record = record.get("input", {})
    execution = record.get("execution", {})
    raw_reference = artifacts.get("raw_host_output")
    raw_path = _resolve_reference(run_dir, raw_reference) if isinstance(raw_reference, str) else None
    if raw_path is not None and raw_path.is_file():
        try:
            raw_output = _read_json(raw_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            raw_output = None
        if not isinstance(raw_output, dict):
            errors.append(f"raw host output is not a valid JSON object: {raw_reference}")

    validated_reference = artifacts.get("validated_result")
    validated_path = (
        _resolve_reference(run_dir, validated_reference)
        if isinstance(validated_reference, str)
        else None
    )
    if validated_path is not None and validated_path.is_file():
        try:
            validated_result = _read_json(validated_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            validated_result = None
        if not isinstance(validated_result, dict) or not isinstance(validated_result.get("findings"), list):
            errors.append(f"validated result must be a JSON object with a findings array: {validated_reference}")

    manifest_reference = input_record.get("review_manifest")
    manifest_path = (
        _resolve_reference(run_dir, manifest_reference)
        if isinstance(manifest_reference, str)
        else None
    )
    if manifest_path is not None and manifest_path.is_file():
        try:
            review_manifest = _read_json(manifest_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            review_manifest = None
        if not isinstance(review_manifest, dict):
            errors.append(f"review manifest must be a JSON object: {manifest_reference}")

    coverage_reference = artifacts.get("coverage_audit")
    if isinstance(coverage_reference, str):
        try:
            _verified_coverage_audit(run_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"coverage audit is invalid: {exc}")

    runtime_reference = artifacts.get("runtime_evidence")
    try:
        runtime_evidence = verify_runtime_evidence(
            run_dir,
            runtime_reference if isinstance(runtime_reference, str) else None,
            record,
        )
    except (OSError, RuntimeEvidenceError, json.JSONDecodeError) as exc:
        errors.append(f"runtime evidence is invalid: {exc}")
    else:
        recorded_runtime = execution.get("runtime_evidence")
        if recorded_runtime is not None and recorded_runtime != runtime_evidence:
            errors.append("execution.runtime_evidence does not match recomputed runtime evidence")
        provenance = record.get("skill_provenance")
        if isinstance(provenance, dict) and provenance.get("verified_loaded") != runtime_evidence.get("verified_loaded"):
            errors.append("skill_provenance.verified_loaded does not match runtime evidence")

    protocol_incidents = execution.get("protocol_incidents", [])
    if not isinstance(protocol_incidents, list):
        errors.append("execution.protocol_incidents must be an array")
    else:
        for incident in protocol_incidents:
            if not isinstance(incident, dict) or not isinstance(incident.get("artifact"), str):
                errors.append("execution.protocol_incidents entries require an artifact path")
                continue
            incident_path = _resolve_reference(run_dir, incident["artifact"])
            if incident_path is None or not incident_path.is_file():
                errors.append(f"protocol incident references a missing file: {incident['artifact']}")
            elif isinstance(incident.get("sha256"), str) and _sha256(incident_path.read_bytes()) != incident["sha256"]:
                errors.append(f"protocol incident SHA-256 does not match: {incident['artifact']}")
    output_ref = artifacts.get("raw_host_output")
    expected_hash = artifacts.get("raw_host_output_sha256")
    output_path = _resolve_reference(run_dir, output_ref) if isinstance(output_ref, str) else None
    if output_path and output_path.is_file() and isinstance(expected_hash, str):
        actual_hash = _sha256(output_path.read_bytes())
        if actual_hash != expected_hash:
            errors.append("raw host output SHA-256 does not match the recorded digest")
    return errors


def _discover_inputs(index: dict[str, Any], runs_root: Path) -> list[tuple[dict[str, Any], str, Path, dict[str, Any]]]:
    discovered: list[tuple[dict[str, Any], str, Path, dict[str, Any]]] = []
    for case in index.get("cases", []):
        for condition in CONDITIONS:
            condition_root = runs_root / case["case_id"] / condition
            for host_path in sorted(condition_root.glob("repeat*/host-output.json")):
                run_dir = host_path.parent
                output = _read_json(host_path)
                if not isinstance(output, dict):
                    raise ValueError(f"Host output is not an object: {host_path}")
                started = _parse_time(output.get("started_at"))
                discovered.append((case, condition, run_dir, {"output": output, "started": started}))
    ordered = sorted(
        discovered,
        key=lambda item: (item[3]["started"] is None, item[3]["started"] or datetime.max),
    )
    order_map = {item[2]: index + 1 for index, item in enumerate(ordered) if item[3]["started"] is not None}
    final: list[tuple[dict[str, Any], str, Path, dict[str, Any]]] = []
    for case, condition, run_dir, metadata in discovered:
        start = _parse_time(metadata["output"].get("started_at"))
        finish = _parse_time(metadata["output"].get("finished_at"))
        concurrent: bool | None = None
        if start is not None and finish is not None:
            concurrent = any(
                other_dir != run_dir
                and (other_start := _parse_time(other_output.get("started_at"))) is not None
                and (other_finish := _parse_time(other_output.get("finished_at"))) is not None
                and start < other_finish
                and other_start < finish
                for _other_case, _other_condition, other_dir, other_meta in discovered
                for other_output in [other_meta["output"]]
            )
        metadata["run_order"] = order_map.get(run_dir)
        metadata["concurrent"] = concurrent
        final.append((case, condition, run_dir, metadata))
    return final


def build_runs(
    index_path: Path,
    runs_root: Path,
    *,
    skill_root: Path,
    planned_replicates: int | None,
    order_seed: str | None,
    skill_label: str,
    skill_revision: str | None,
    skill_bundle_sha256: str | None,
    skill_bundle_path: Path | None,
) -> list[Path]:
    index = _read_json(index_path)
    if not isinstance(index, dict) or not isinstance(index.get("cases"), list):
        raise ValueError("Case index must contain a cases array")
    blind_root = Path(index["blind_root"])
    operator_root = Path(index["operator_root"])
    discovered = _discover_inputs(index, runs_root)
    for _case, _condition, run_dir, _metadata in discovered:
        _ensure_unrecorded_attempt(run_dir)

    result_paths: list[Path] = []
    for case, condition, run_dir, metadata in discovered:
        record = build_run_record(
            index,
            case,
            condition,
            run_dir,
            blind_root=blind_root,
            operator_root=operator_root,
            skill_root=skill_root,
            run_order=metadata["run_order"],
            concurrent=metadata["concurrent"],
            planned_replicates=planned_replicates,
            order_seed=order_seed,
            skill_label=skill_label,
            skill_revision=skill_revision,
            skill_bundle_sha256=skill_bundle_sha256,
            skill_bundle_path=skill_bundle_path,
        )
        record_path = run_dir / "run.json"
        errors = validate_record(record, run_dir)
        if errors:
            raise ValueError(f"Generated invalid run record {record_path}: {'; '.join(errors)}")
        score = _score_run_record(record, run_dir, case)
        score_path = run_dir / "score.json"
        record["scoring"] = {
            "status": "completed",
            "script": "eval/ab_eval.py",
            "version": VERSION,
            "output": _relative(score_path, run_dir),
            "dependency_manifest_version": 1,
            "dependency_fingerprint": score["dependencies"]["fingerprint"],
        }
        _write_json(score_path, score)
        _write_json(record_path, record)
        result_paths.append(record_path)
    return result_paths


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _same_known_value(left: Any, right: Any) -> bool | None:
    if left is None or right is None:
        return None
    return left == right


def summarize(run_paths: list[Path], expected_cases: list[str] | None = None) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    scores: dict[Path, dict[str, Any]] = {}
    invalid_runs: list[str] = []
    for path in run_paths:
        record = _read_json(path)
        if not isinstance(record, dict):
            invalid_runs.append(f"{path}: run record must be a JSON object")
            continue
        errors = validate_record(record, path.parent)
        if errors:
            invalid_runs.append(f"{path}: {'; '.join(errors)}")
            continue
        records.append(record)
    if invalid_runs:
        raise ValueError("Cannot summarize invalid run records:\n" + "\n".join(invalid_runs))
    for path, record in zip(run_paths, records):
        scores[path] = _score_run_record(record, path.parent)

    observed_conditions: dict[str, list[dict[str, Any]]] = {condition: [] for condition in CONDITIONS}
    for record in records:
        condition = record.get("condition")
        if condition in observed_conditions:
            observed_conditions[condition].append(record)
    by_condition: dict[str, Any] = {}
    for condition, condition_records in observed_conditions.items():
        condition_scores = [scores[path] for path, record in zip(run_paths, records) if record.get("condition") == condition]
        durations = [
            score["workflow"].get("duration_seconds")
            for score in condition_scores
            if isinstance(score["workflow"].get("duration_seconds"), (int, float))
        ]
        precisions = [
            score["human_adjudication"].get("precision")
            for score in condition_scores
            if isinstance(score["human_adjudication"].get("precision"), (int, float))
        ]
        recalls = [
            score["human_adjudication"].get("known_defect_recall")
            for score in condition_scores
            if isinstance(score["human_adjudication"].get("known_defect_recall"), (int, float))
        ]
        completed = [record for record in condition_records if record.get("run_status") == "completed"]
        by_condition[condition] = {
            "runs_observed": len(condition_records),
            "runs_completed": len(completed),
            "workflow_completion_rate_observed": len(completed) / len(condition_records) if condition_records else None,
            "host_reported_completed_count": len(completed),
            "supervisor_trace_success_count": sum(
                score["workflow"].get("supervisor_trace_status") == "success"
                for score in condition_scores
            ),
            "supervisor_trace_failed_count": sum(
                score["workflow"].get("supervisor_trace_status") == "failed"
                for score in condition_scores
            ),
            "supervisor_trace_unknown_count": sum(
                score["workflow"].get("supervisor_trace_status") == "unknown"
                for score in condition_scores
            ),
            "service_identity_evidence": "host_reported_unverified",
            "duration_seconds_mean_known_only": _mean([float(value) for value in durations]),
            "duration_known_count": len(durations),
            "human_precision_mean": _mean([float(value) for value in precisions]),
            "human_precision_case_count": len(precisions),
            "known_defect_recall_mean": _mean([float(value) for value in recalls]),
            "known_defect_recall_case_count": len(recalls),
            "unknown_cost_count": sum(score["model_usage"].get("cost") is None for score in condition_scores),
        }

    pairs: list[dict[str, Any]] = []
    case_ids = expected_cases or sorted({record.get("case_id") for record in records})
    for case_id in case_ids:
        all_condition_runs: dict[str, list[tuple[Path, dict[str, Any], dict[str, Any]]]] = {
            condition: [] for condition in CONDITIONS
        }
        for path, record in zip(run_paths, records):
            if record.get("case_id") == case_id and record.get("condition") in all_condition_runs:
                all_condition_runs[record["condition"]].append((path, record, scores[path]))
        replicates = sorted({
            record.get("comparison", {}).get("replicate")
            for values in all_condition_runs.values()
            for _path, record, _score in values
            if isinstance(record.get("comparison", {}).get("replicate"), int)
        })
        if not replicates:
            replicates = [None]

        for replicate in replicates:
            condition_runs = {
                condition: [
                    item for item in values
                    if item[1].get("comparison", {}).get("replicate") == replicate
                ]
                for condition, values in all_condition_runs.items()
            }

            def choose_attempt(items: list[tuple[Path, dict[str, Any], dict[str, Any]]]) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
                if not items:
                    return None
                completed = [item for item in items if item[1].get("run_status") == "completed"]
                pool = completed or items
                return max(
                    pool,
                    key=lambda item: (
                        item[1].get("comparison", {}).get("attempt") or 0,
                        item[1].get("comparison", {}).get("run_order") or 0,
                    ),
                )

            direct = choose_attempt(condition_runs["same_host_direct"])
            skill = choose_attempt(condition_runs["current_skill"])
            if not direct or not skill:
                pairs.append({
                    "case_id": case_id,
                    "replicate": replicate,
                    "status": "pair_incomplete",
                    "conditions_present": [key for key, value in condition_runs.items() if value],
                    "attempts": {key: len(value) for key, value in condition_runs.items()},
                })
                continue
            _, direct_record, direct_score = direct
            _, skill_record, skill_score = skill
            direct_input = direct_record.get("input", {})
            skill_input = skill_record.get("input", {})
            input_checks = {
                key: _same_known_value(direct_input.get(key), skill_input.get(key))
                for key in (
                    "repository_id", "snapshot_fingerprint", "review_scope_fingerprint",
                    "review_manifest_sha256",
                )
            }
            host_fields = ("name", "version", "model", "configuration_fingerprint")
            host_checks = {
                key: _same_known_value(direct_record.get("host", {}).get(key), skill_record.get("host", {}).get(key))
                for key in host_fields
            }
            input_mismatch = any(value is False for value in input_checks.values())
            host_mismatch = any(value is False for value in host_checks.values())
            input_complete = all(value is True for value in input_checks.values())
            host_complete = all(value is True for value in host_checks.values())
            direct_runtime = direct_score["workflow"]
            skill_runtime = skill_score["workflow"]
            common_bindings = {
                name: _same_known_value(
                    (direct_runtime.get("common_bindings") or {}).get(name),
                    (skill_runtime.get("common_bindings") or {}).get(name),
                )
                for name in (
                    "review_manifest", "common_prompt", "output_schema", "runtime_config"
                )
            }
            common_binding_mismatch = any(value is False for value in common_bindings.values())
            common_bindings_complete = (
                all(value is True for value in common_bindings.values())
                and direct_runtime.get("common_binding_status") == "verified"
                and skill_runtime.get("common_binding_status") == "verified"
            )
            runtime_evidence_checks = {
                "same_host_direct": direct_runtime.get("runtime_evidence_status") == "verified",
                "current_skill": skill_runtime.get("runtime_evidence_status") == "verified",
            }
            supervisor_trace_checks = {
                "same_host_direct": direct_runtime.get("supervisor_trace_status") == "success",
                "current_skill": skill_runtime.get("supervisor_trace_status") == "success",
            }
            runtime_evidence_complete = all(runtime_evidence_checks.values())
            supervisor_traces_successful = all(supervisor_trace_checks.values())
            direct_build_check = direct_record.get("repository_snapshot", {}).get("build_check", {})
            skill_build_check = skill_record.get("repository_snapshot", {}).get("build_check", {})
            direct_score_check = direct_score["workflow"].get("repository_snapshot_check", {})
            skill_score_check = skill_score["workflow"].get("repository_snapshot_check", {})
            direct_fingerprint_check = direct_score["workflow"].get("input_fingerprint_check", {})
            skill_fingerprint_check = skill_score["workflow"].get("input_fingerprint_check", {})
            repository_checks = {
                "same_host_direct": {
                    "build": direct_build_check.get("status"),
                    "scoring": direct_score_check.get("status"),
                    "input_fingerprints": direct_fingerprint_check.get("status"),
                },
                "current_skill": {
                    "build": skill_build_check.get("status"),
                    "scoring": skill_score_check.get("status"),
                    "input_fingerprints": skill_fingerprint_check.get("status"),
                },
            }
            repository_complete = all(
                check == "verified"
                for condition_checks in repository_checks.values()
                for check in condition_checks.values()
            )
            raw_skill_provenance = skill_record.get("skill_provenance")
            skill_provenance = raw_skill_provenance if isinstance(raw_skill_provenance, dict) else {}
            verified_loaded = skill_runtime.get("skill_load_verified")
            skill_evidence_status = (
                "missing_runtime_evidence" if skill_runtime.get("runtime_evidence_status") == "missing"
                else "verified_loaded" if verified_loaded is True
                else "digest_mismatch" if skill_runtime.get("skill_load_status") == "digest_mismatch"
                else "missing_or_incomplete"
            )
            runtime_protocol_violations = [
                condition
                for condition, workflow in (
                    ("same_host_direct", direct_runtime), ("current_skill", skill_runtime)
                )
                if workflow.get("runtime_protocol_violation") is True
            ]
            protocol_incidents = [
                incident
                for run_record in (direct_record, skill_record)
                for incident in run_record.get("execution", {}).get("protocol_incidents", [])
                if isinstance(incident, dict)
            ]
            confirmed_protocol_incidents = [
                incident for incident in protocol_incidents
                if incident.get("raw_host_output_sha256_matches") is True
            ]
            if confirmed_protocol_incidents or runtime_protocol_violations:
                pair_status = "inconclusive_protocol_violation"
            elif input_mismatch or host_mismatch or not input_complete or not repository_complete:
                pair_status = "not_comparable"
            elif common_binding_mismatch:
                pair_status = "not_comparable_common_runtime_bindings_mismatch"
            elif not common_bindings_complete:
                pair_status = "not_comparable_common_runtime_bindings_incomplete"
            elif not runtime_evidence_complete:
                pair_status = "not_comparable_runtime_evidence_incomplete"
            elif not supervisor_traces_successful:
                pair_status = "not_comparable_supervisor_trace_not_successful"
            elif verified_loaded is False:
                pair_status = "not_comparable_skill_bundle_digest_mismatch"
            elif verified_loaded is None:
                pair_status = (
                    "paired_inputs_and_host_identity_verified_skill_load_unverified"
                    if host_complete
                    else "paired_inputs_verified_host_identity_incomplete_skill_load_unverified"
                )
            elif host_complete:
                pair_status = "paired_inputs_and_host_identity_verified"
            else:
                pair_status = "paired_inputs_verified_host_identity_incomplete"
            pairs.append({
                "case_id": case_id,
                "replicate": replicate,
                "status": pair_status,
                "selected_attempts": {
                    "same_host_direct": direct_record.get("comparison", {}).get("attempt"),
                    "current_skill": skill_record.get("comparison", {}).get("attempt"),
                },
                "attempt_history": {
                    key: [
                        {
                            "attempt": record.get("comparison", {}).get("attempt"),
                            "attempt_kind": record.get("comparison", {}).get("attempt_kind"),
                            "run_status": record.get("run_status"),
                            "review_status": record.get("execution", {}).get("review_status"),
                            "duration_seconds": scores[path]["workflow"].get("duration_seconds"),
                        }
                        for path, record, _score in value
                    ]
                    for key, value in condition_runs.items()
                },
                "input_checks": input_checks,
                "common_runtime_binding_checks": common_bindings,
                "runtime_evidence_checks": runtime_evidence_checks,
                "supervisor_trace_checks": supervisor_trace_checks,
                "host_checks": host_checks,
                "service_identity": {
                    "verified": None,
                    "evidence_status": "host_reported_unverified",
                    "host_reported_model": skill_record.get("host", {}).get("model"),
                },
                "repository_snapshot_checks": repository_checks,
                "skill_load_check": {
                    "verified_loaded": verified_loaded,
                    "evidence_status": skill_evidence_status,
                    "provided_bundle_sha256": skill_provenance.get("provided_bundle_sha256"),
                    "bundle_sha256": skill_provenance.get("bundle_sha256"),
                    "host_reported_bundle_sha256": skill_provenance.get("host_reported_bundle_sha256"),
                    "host_reported_bundle_digest_status": skill_provenance.get("host_reported_bundle_digest_status"),
                    "runtime_evidence_status": skill_runtime.get("runtime_evidence_status"),
                    "supervisor_trace_status": skill_runtime.get("supervisor_trace_status"),
                    "skill_load_status": skill_runtime.get("skill_load_status"),
                },
                "protocol_incidents": protocol_incidents,
                "runtime_protocol_violations": runtime_protocol_violations,
                "workflow_completed": {
                    "same_host_direct": direct_score["workflow"].get("execution_completed"),
                    "current_skill": skill_score["workflow"].get("execution_completed"),
                },
                "completion_evidence": {
                    "same_host_direct": {
                        "host_reported_completed": direct_runtime.get("host_reported_completed"),
                        "supervisor_trace_status": direct_runtime.get("supervisor_trace_status"),
                        "runtime_evidence_status": direct_runtime.get("runtime_evidence_status"),
                    },
                    "current_skill": {
                        "host_reported_completed": skill_runtime.get("host_reported_completed"),
                        "supervisor_trace_status": skill_runtime.get("supervisor_trace_status"),
                        "runtime_evidence_status": skill_runtime.get("runtime_evidence_status"),
                    },
                },
                "coverage_complete": {
                    "same_host_direct": direct_score["workflow"].get("coverage_complete"),
                    "current_skill": skill_score["workflow"].get("coverage_complete"),
                },
                "reported_coverage_complete_unverified": {
                    "same_host_direct": direct_score["workflow"].get("reported_coverage_complete_unverified"),
                    "current_skill": skill_score["workflow"].get("reported_coverage_complete_unverified"),
                },
                "skill_bundle_loaded_verified": verified_loaded,
                "finding_count": {
                    "same_host_direct": direct_score["workflow"].get("finding_count"),
                    "current_skill": skill_score["workflow"].get("finding_count"),
                },
                "human_precision": {
                    "same_host_direct": direct_score["human_adjudication"].get("precision"),
                    "current_skill": skill_score["human_adjudication"].get("precision"),
                },
                "known_defect_recall": {
                    "same_host_direct": direct_score["human_adjudication"].get("known_defect_recall"),
                    "current_skill": skill_score["human_adjudication"].get("known_defect_recall"),
                },
                "duration_seconds": {
                    "same_host_direct": direct_score["workflow"].get("duration_seconds"),
                    "current_skill": skill_score["workflow"].get("duration_seconds"),
                },
                "limitations": [
                    "Selected result is the latest completed attempt within this replicate; all attempts remain in history.",
                    "Unknown host or model configuration is retained as unknown.",
                    "Host-reported model/version do not verify the remote service identity.",
                    "Host-reported read paths do not independently verify review coverage.",
                    "Build/scoring snapshot checks cannot establish that the repository remained unchanged during review.",
                ] if not host_complete else [
                    "Selected result is the latest completed attempt within this replicate; all attempts remain in history.",
                    "Host-reported model/version do not verify the remote service identity.",
                    "Host-reported read paths do not independently verify review coverage.",
                    "Build/scoring snapshot checks cannot establish that the repository remained unchanged during review.",
                ],
            })

    expected = {(case_id, condition) for case_id in case_ids for condition in CONDITIONS}
    observed = {(record.get("case_id"), record.get("condition")) for record in records}
    missing = [
        {"case_id": case_id, "condition": condition, "status": "no_run_record"}
        for case_id, condition in sorted(expected - observed)
    ]
    case_sources: dict[str, str | None] = {}
    adjudicated_case_ids: set[str] = set()
    pending_case_ids: set[str] = set()
    for record in records:
        case_id = record.get("case_id")
        if not isinstance(case_id, str):
            continue
        case_sources.setdefault(case_id, record.get("input", {}).get("repository_id"))
        if record.get("annotation", {}).get("status") == "adjudicated":
            adjudicated_case_ids.add(case_id)
        else:
            pending_case_ids.add(case_id)
    source_case_counts: dict[str, int] = {}
    for source in case_sources.values():
        key = source or "unknown"
        source_case_counts[key] = source_case_counts.get(key, 0) + 1
    return {
        "schema_version": 1,
        "benchmark_id": records[0].get("benchmark_id") if records else None,
        "case_origin": "real_repository" if all(r.get("case_origin") == "real_repository" for r in records) else "mixed_or_unknown",
        "run_count": len(records),
        "case_source_counts": source_case_counts,
        "adjudicated_case_count": len(adjudicated_case_ids),
        "pending_annotation_case_count": len(pending_case_ids - adjudicated_case_ids),
        "independent_case_groups": sorted({
            record.get("comparison", {}).get("case_group_id")
            or record.get("case_id")
            for record in records
        }),
        "by_condition": by_condition,
        "paired_cases": pairs,
        "missing_expected_runs": missing,
        "not_included_conditions": ["context_selector_skill"],
        "interpretation": [
            "Workflow completion and review coverage are execution measures, not review correctness.",
            "Host-reported read paths do not prove actual file reads; coverage is unknown until independently audited.",
            "Unlabelled findings are pending and are not counted as false positives.",
            "Pending clean-case candidates are excluded from clean-case false-alarm denominators.",
            "Operator or agent evidence checks are provisional and are not human precision or recall.",
            "A verified protocol violation makes the pair inconclusive, even when both saved outputs are available.",
            "Build/scoring snapshot checks cannot establish that the repository remained unchanged during review.",
            "Unknown time, intervention, token, and cost measurements remain null or are summarized only with a known-count.",
            "When cases are concentrated in one source repository, results have limited external generalization.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# A/B Review Report: {report.get('benchmark_id') or 'unknown'}",
        "",
        f"Observed runs: {report['run_count']}",
        f"Independent case groups: {len(report['independent_case_groups'])}",
        "Source cases: " + ", ".join(
            f"{source}={count}" for source, count in sorted(report.get("case_source_counts", {}).items())
        ),
        f"Human adjudication: {report.get('adjudicated_case_count', 0)} cases adjudicated; {report.get('pending_annotation_case_count', 0)} pending.",
        "",
        "| Condition | Runs | Completed | Completion rate | Mean known duration (s) | Human precision | Known defect recall |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for condition, values in report["by_condition"].items():
        lines.append(
            "| {condition} | {runs} | {completed} | {rate} | {duration} | {precision} | {recall} |".format(
                condition=condition,
                runs=values["runs_observed"],
                completed=values["runs_completed"],
                rate=_fmt(values["workflow_completion_rate_observed"]),
                duration=_fmt(values["duration_seconds_mean_known_only"]),
                precision=_fmt(values["human_precision_mean"]),
                recall=_fmt(values["known_defect_recall_mean"]),
            )
        )
    lines.extend(["", "## Paired Cases", ""])
    if not report["paired_cases"]:
        lines.append("No case pair is available.")
    for pair in report["paired_cases"]:
        replicate = pair.get("replicate")
        suffix = f" / replicate {replicate}" if replicate is not None else ""
        lines.append(f"- `{pair['case_id']}`{suffix}: **{pair['status']}**")
        if "finding_count" in pair:
            lines.append(
                "  Reported findings: direct={direct}, Skill={skill}; human precision/recall remain null until adjudicated.".format(
                    direct=_fmt(pair["finding_count"].get("same_host_direct")),
                    skill=_fmt(pair["finding_count"].get("current_skill")),
                )
            )
    lines.extend(["", "## Missing Runs", ""])
    if report["missing_expected_runs"]:
        lines.extend(
            f"- `{item['case_id']}` / `{item['condition']}`: {item['status']}"
            for item in report["missing_expected_runs"]
        )
    else:
        lines.append("None among the expected A/B conditions.")
    lines.extend(["", "## Limits", ""])
    lines.extend(f"- {item}" for item in report["interpretation"])
    lines.append("")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="Build records and scores from saved host outputs")
    build_parser.add_argument("--index", type=Path, required=True)
    build_parser.add_argument("--runs-root", type=Path, required=True)
    build_parser.add_argument("--skill-root", type=Path, default=ROOT / "skills" / "review-changes")
    build_parser.add_argument("--planned-replicates", type=int, default=None)
    build_parser.add_argument("--order-seed", default=None)
    build_parser.add_argument("--skill-label", default="development-bundle")
    build_parser.add_argument("--skill-revision", default=None)
    build_parser.add_argument("--skill-bundle-sha256", default=None)
    build_parser.add_argument("--skill-bundle", type=Path, default=None)

    validate_parser = subparsers.add_parser("validate", help="Validate a run record and referenced artifacts")
    validate_parser.add_argument("run", type=Path)

    score_parser = subparsers.add_parser("score", help="Score one saved run without model calls")
    score_parser.add_argument("run", type=Path)

    summary_parser = subparsers.add_parser("summarize", help="Create a paired descriptive A/B report")
    summary_parser.add_argument("--index", type=Path, required=True)
    summary_parser.add_argument("--runs-root", type=Path, required=True)
    summary_parser.add_argument("--output", type=Path, default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            paths = build_runs(
                args.index.resolve(),
                args.runs_root.resolve(),
                skill_root=args.skill_root.resolve(),
                planned_replicates=args.planned_replicates,
                order_seed=args.order_seed,
                skill_label=args.skill_label,
                skill_revision=args.skill_revision,
                skill_bundle_sha256=args.skill_bundle_sha256,
                skill_bundle_path=args.skill_bundle.resolve() if args.skill_bundle else None,
            )
            print(json.dumps({"created": [str(path) for path in paths]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate":
            path = args.run.resolve()
            errors = validate_record(_read_json(path), path.parent)
            print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
            return 0 if not errors else 2
        if args.command == "score":
            path = args.run.resolve()
            score = score_record(path)
            destination = path.parent / "score.json"
            _write_json(destination, score)
            record = _scoring_record_snapshot(_read_json(path))
            record["scoring"] = {
                "status": "completed",
                "script": "eval/ab_eval.py",
                "version": VERSION,
                "output": _relative(destination, path.parent),
                "dependency_manifest_version": 1,
                "dependency_fingerprint": score["dependencies"]["fingerprint"],
            }
            _write_json(path, record)
            print(json.dumps(score, ensure_ascii=False, indent=2))
            return 0
        if args.command == "summarize":
            index = _read_json(args.index.resolve())
            run_paths = sorted(args.runs_root.resolve().rglob("run.json"))
            report = summarize(run_paths, [case["case_id"] for case in index.get("cases", [])])
            destination = args.output or (args.runs_root.resolve() / "ab-report.json")
            _write_json(destination, report)
            destination.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
            print(json.dumps({"report": str(destination), "runs": len(run_paths)}, indent=2))
            return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"ab_eval: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
