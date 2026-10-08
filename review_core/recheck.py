"""Deterministic association of a prior review with a new review snapshot."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from review_core.validation import ReviewValidationError, validate_manifest, validate_review


_STATUSES = {"resolved", "persisting", "unverified"}


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _semantic_key(finding: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(
        _text(finding.get(field))
        for field in ("category", "root_cause", "trigger", "impact")
    )


def _manifest_sources(manifest: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    sources: dict[tuple[str, str], dict[str, Any]] = {}
    for item in [*manifest.get("files", []), *manifest.get("context_files", [])]:
        for side in ("before", "after"):
            snapshot = item.get(side, {})
            path = snapshot.get("path")
            if path is not None:
                sources[(path, side)] = snapshot
    return sources


def _path_aliases(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    for manifest in (previous, current):
        for item in manifest.get("files", []):
            names = {item.get("filename")}
            if item.get("previous_filename"):
                names.add(item["previous_filename"])
            names.discard(None)
            for name in names:
                aliases.setdefault(name, set()).update(names)
    for name in list(aliases):
        component = {name}
        pending = [name]
        while pending:
            current_name = pending.pop()
            for alias in aliases.get(current_name, set()) - component:
                component.add(alias)
                pending.append(alias)
        for alias in component:
            aliases[alias] = set(component)
    return aliases


def _evidence_paths(finding: dict[str, Any], aliases: dict[str, set[str]]) -> set[str]:
    paths: set[str] = set()
    for evidence in finding.get("evidence", []):
        path = evidence.get("file")
        if isinstance(path, str):
            paths.add(path)
            paths.update(aliases.get(path, set()))
    return paths


def _evidence_valid(evidence: Any, sources: dict[tuple[str, str], dict[str, Any]]) -> bool:
    if not isinstance(evidence, list) or not evidence:
        return False
    for item in evidence:
        if not isinstance(item, dict):
            return False
        file = item.get("file")
        side = item.get("side")
        start = item.get("line_start")
        end = item.get("line_end")
        fingerprint = item.get("snapshot_fingerprint")
        description = item.get("description")
        if (
            not isinstance(file, str)
            or not isinstance(side, str)
            or side not in {"before", "after"}
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(fingerprint, str)
            or not isinstance(description, str)
            or not description.strip()
        ):
            return False
        snapshot = sources.get((file, side))
        if snapshot is None:
            return False
        content = snapshot.get("content")
        if not isinstance(content, str) or start < 1 or end < start:
            return False
        if end > len(content.splitlines()):
            return False
        if fingerprint != snapshot.get("fingerprint"):
            return False
    return True


def _decision_map(
    current_result: dict[str, Any],
    sources: dict[tuple[str, str], dict[str, Any]],
    previous_ids: set[str],
) -> dict[str, dict[str, Any]]:
    raw = current_result.get("recheck_decisions", [])
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ReviewValidationError("result.recheck_decisions must be an array")
    decisions: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ReviewValidationError(f"result.recheck_decisions[{index}] must be an object")
        finding_id = item.get("previous_finding_id")
        status = item.get("status")
        reason = item.get("reason")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ReviewValidationError(f"result.recheck_decisions[{index}].previous_finding_id is required")
        if finding_id not in previous_ids:
            raise ReviewValidationError(
                f"result.recheck_decisions[{index}].previous_finding_id does not identify a prior finding"
            )
        if finding_id in decisions:
            raise ReviewValidationError(
                f"result.recheck_decisions[{index}].previous_finding_id is duplicated"
            )
        if not isinstance(status, str) or status not in _STATUSES:
            raise ReviewValidationError(f"result.recheck_decisions[{index}].status is invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise ReviewValidationError(f"result.recheck_decisions[{index}].reason is required")
        evidence = item.get("evidence", [])
        evidence_valid = _evidence_valid(evidence, sources)
        decisions[finding_id] = {
            "previous_finding_id": finding_id,
            "status": status,
            "reason": reason,
            "evidence": copy.deepcopy(evidence) if evidence_valid else [],
            "evidence_valid": evidence_valid,
        }
    return decisions


def _associate(
    previous: dict[str, Any],
    current: dict[str, Any],
    previous_manifest: dict[str, Any],
    current_manifest: dict[str, Any],
) -> tuple[dict[str, tuple[dict[str, Any], str]], set[str]]:
    aliases = _path_aliases(previous_manifest, current_manifest)
    current_findings = current.get("findings", [])
    current_by_id = {item["finding_id"]: item for item in current_findings}
    associated: dict[str, tuple[dict[str, Any], str]] = {}
    reserved_current_ids: set[str] = set()

    # Stable IDs own their matches before any heuristic association is tried.
    for prior in previous.get("findings", []):
        prior_id = prior["finding_id"]
        candidate = current_by_id.get(prior_id)
        if candidate is not None:
            associated[prior_id] = (candidate, candidate.get("validation_status", "needs_confirmation"))
            reserved_current_ids.add(candidate["finding_id"])

    remaining_current = {
        item["finding_id"]: item
        for item in current_findings
        if item["finding_id"] not in reserved_current_ids
    }
    remaining_prior = [
        item for item in previous.get("findings", [])
        if item["finding_id"] not in associated
    ]
    candidates_by_prior: dict[str, set[str]] = {}
    for prior in remaining_prior:
        same = {
            item_id
            for item_id, item in remaining_current.items()
            if _semantic_key(item) == _semantic_key(prior)
        }
        prior_paths = _evidence_paths(prior, aliases)
        same_with_path = {
            item_id
            for item_id in same
            if prior_paths and _evidence_paths(remaining_current[item_id], aliases) & prior_paths
        }
        candidates_by_prior[prior["finding_id"]] = same_with_path or same

    request_count: dict[str, int] = {}
    for candidates in candidates_by_prior.values():
        for candidate_id in candidates:
            request_count[candidate_id] = request_count.get(candidate_id, 0) + 1

    ambiguous: set[str] = set()
    for prior in remaining_prior:
        prior_id = prior["finding_id"]
        candidates = candidates_by_prior[prior_id]
        if len(candidates) != 1:
            if candidates:
                ambiguous.add(prior_id)
            continue
        candidate_id = next(iter(candidates))
        if request_count[candidate_id] != 1:
            ambiguous.add(prior_id)
            continue
        candidate = remaining_current[candidate_id]
        associated[prior_id] = (candidate, candidate.get("validation_status", "needs_confirmation"))
    return associated, ambiguous


def _path_names(path: str, aliases: dict[str, set[str]]) -> set[str]:
    return aliases.get(path, {path})


def _related_coverage_problem(
    prior: dict[str, Any],
    decision: dict[str, Any],
    current_manifest: dict[str, Any],
    current: dict[str, Any],
    aliases: dict[str, set[str]],
) -> str | None:
    related_paths = _evidence_paths(prior, aliases) | _evidence_paths(decision, aliases)
    coverage = {item["file"]: item for item in current.get("coverage", [])}
    for item in current_manifest.get("files", []):
        item_paths = {item.get("filename"), item.get("previous_filename")}
        item_paths.discard(None)
        expanded = set(item_paths)
        for path in item_paths:
            expanded.update(_path_names(path, aliases))
        if not expanded & related_paths:
            continue
        report = coverage.get(item["filename"])
        if report is None:
            return f"Related selected file '{item['filename']}' has no current coverage record."
        if report["semantic_status"] != "completed":
            return (
                f"Related selected file '{item['filename']}' has "
                f"{report['semantic_status']} semantic coverage."
            )
        if report["context_gaps"]:
            return f"Related selected file '{item['filename']}' has unresolved context gaps."
    return None


def _materially_changed(
    evidence: dict[str, Any],
    prior: dict[str, Any],
    previous_sources: dict[tuple[str, str], dict[str, Any]],
    current_sources: dict[tuple[str, str], dict[str, Any]],
    aliases: dict[str, set[str]],
) -> bool:
    if evidence.get("side") != "after":
        return False
    current_snapshot = current_sources.get((evidence.get("file"), "after"), {})
    current_content = current_snapshot.get("content")
    if not isinstance(current_content, str):
        return False
    old_paths = [
        item["file"]
        for item in prior.get("evidence", [])
        if evidence["file"] in _path_names(item["file"], aliases)
    ]
    old_paths.extend(
        path
        for path in sorted(_path_names(evidence["file"], aliases))
        if path not in old_paths
    )
    for old_path in old_paths:
        previous_after = previous_sources.get((old_path, "after"))
        if previous_after is not None:
            old_content = previous_after.get("content")
            if not isinstance(old_content, str):
                return False
            return old_content != current_content
    for old_path in old_paths:
        old_content = previous_sources.get((old_path, "before"), {}).get("content")
        if isinstance(old_content, str):
            return old_content != current_content
    return False


def _deleted_file_repair_evidence(
    evidence: list[dict[str, Any]],
    prior_paths: set[str],
    current_manifest: dict[str, Any],
    aliases: dict[str, set[str]],
) -> bool:
    deleted_paths: set[str] = set()
    for item in current_manifest.get("files", []):
        if item.get("status") in {"removed", "deleted", "delete"}:
            for key in ("filename", "previous_filename"):
                if item.get(key):
                    deleted_paths.add(item[key])
                    deleted_paths.update(_path_names(item[key], aliases))
    cites_deleted_before = any(
        item.get("side") == "before"
        and item.get("file") in deleted_paths
        and item.get("file") in prior_paths
        for item in evidence
    )
    cites_current_caller = any(
        item.get("side") == "after"
        and item.get("file") not in deleted_paths
        and item.get("file") in prior_paths
        for item in evidence
    )
    return cites_deleted_before and cites_current_caller


def _prior_path_review_problem(
    prior: dict[str, Any],
    decision: dict[str, Any],
    current_sources: dict[tuple[str, str], dict[str, Any]],
    current_manifest: dict[str, Any],
    aliases: dict[str, set[str]],
) -> str | None:
    evidence = decision["evidence"]
    prior_paths = _evidence_paths(prior, aliases)
    deleted_paths: set[str] = set()
    for item in current_manifest.get("files", []):
        if item.get("status") in {"removed", "deleted", "delete"}:
            for key in ("filename", "previous_filename"):
                if item.get(key):
                    deleted_paths.add(item[key])
                    deleted_paths.update(_path_names(item[key], aliases))

    for prior_evidence in prior.get("evidence", []):
        path = prior_evidence["file"]
        names = _path_names(path, aliases)
        current_after = any(
            (name, "after") in current_sources
            and isinstance(current_sources[(name, "after")].get("content"), str)
            for name in names
        )
        if current_after:
            if not any(
                item.get("side") == "after" and item.get("file") in names
                for item in evidence
            ):
                return f"Current resolution evidence does not show reinspection of prior path '{path}'."
            continue
        deleted_names = names & deleted_paths
        has_deleted_before = bool(deleted_names) and any(
            (name, "before") in current_sources
            and isinstance(current_sources[(name, "before")].get("content"), str)
            for name in deleted_names
        )
        if has_deleted_before:
            if not any(
                item.get("side") == "before" and item.get("file") in deleted_names
                for item in evidence
            ):
                return f"Current resolution evidence must cite deleted prior path '{path}' on its before side."
            if any(
                item.get("side") == "after"
                and item.get("file") in prior_paths
                and item.get("file") not in deleted_paths
                for item in evidence
            ):
                continue
            return f"Deleted prior path '{path}' also requires current after-side caller or replacement evidence."
        return f"Prior evidence path '{path}' has no accessible current after snapshot."
    return None


def _resolution_block_reason(
    prior: dict[str, Any],
    decision: dict[str, Any],
    current_manifest: dict[str, Any],
    previous_sources: dict[tuple[str, str], dict[str, Any]],
    current_sources: dict[tuple[str, str], dict[str, Any]],
    current: dict[str, Any],
    aliases: dict[str, set[str]],
) -> str | None:
    if not decision.get("evidence_valid"):
        return (
            "Resolution evidence is missing or malformed; cite current snapshots with a path, "
            "side, valid lines, fingerprint, and description."
        )

    evidence = decision["evidence"]
    prior_path_problem = _prior_path_review_problem(
        prior, decision, current_sources, current_manifest, aliases
    )
    if prior_path_problem:
        return prior_path_problem
    prior_paths = _evidence_paths(prior, aliases)

    coverage_problem = _related_coverage_problem(
        prior, decision, current_manifest, current, aliases
    )
    if coverage_problem:
        return coverage_problem

    if not any(item.get("side") == "after" for item in evidence):
        return "Resolution evidence must include a current after-side repair citation."
    if any(
        _path_names(item["file"], aliases) & prior_paths
        and _materially_changed(item, prior, previous_sources, current_sources, aliases)
        for item in evidence
    ):
        return None
    if _deleted_file_repair_evidence(evidence, prior_paths, current_manifest, aliases):
        return None
    return (
        "Resolution evidence does not show a detectable captured source change relative to the prior snapshot."
    )


def recheck_review(
    previous_result: dict[str, Any],
    previous_manifest: dict[str, Any],
    current_manifest: dict[str, Any],
    current_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare a prior validated result with a new snapshot and result.

    Resolution requires an explicit host decision, valid current evidence,
    relevant complete coverage, and a detectable source change. Semantic
    fallback association uses normalized exact field equality, not similarity.
    """
    previous_manifest = validate_manifest(previous_manifest)
    current_manifest = validate_manifest(current_manifest)
    previous = validate_review(previous_result, previous_manifest)
    current: dict[str, Any] | None = None
    if current_result is not None:
        current = validate_review(current_result, current_manifest)

    previous_report_fingerprint = _fingerprint(previous)
    aliases = _path_aliases(previous_manifest, current_manifest)
    previous_sources = _manifest_sources(previous_manifest)
    current_sources = _manifest_sources(current_manifest)
    previous_ids = {item["finding_id"] for item in previous.get("findings", [])}
    decisions = _decision_map(current or {}, current_sources, previous_ids)
    associated, ambiguous = _associate(
        previous, current or {"findings": []}, previous_manifest, current_manifest
    )
    semantic_result_unavailable = current is None or current.get("review_status") in {"failed", "uncovered"}

    statuses: list[dict[str, Any]] = []
    matched_ids: set[str] = set()
    for prior in previous.get("findings", []):
        finding_id = prior["finding_id"]
        association = associated.get(finding_id)
        if association is not None:
            matched_ids.add(association[0]["finding_id"])
        if semantic_result_unavailable:
            status = "unverified"
            reason = (
                "No current semantic result was supplied; absence from a report does not prove resolution."
                if current is None
                else "The current semantic review failed or was uncovered; resolution cannot be verified."
            )
            evidence = []
        elif finding_id in decisions:
            decision = decisions[finding_id]
            status = decision["status"]
            reason = decision["reason"]
            evidence = decision["evidence"]
            if status == "resolved":
                candidate = association[0] if association is not None else None
                if finding_id in ambiguous:
                    status = "unverified"
                    reason = (
                        "Multiple findings could match this prior issue; the association is ambiguous, "
                        "so the resolved decision cannot be verified."
                    )
                elif candidate is not None and candidate.get("validation_status") == "confirmed":
                    status = "unverified"
                    reason = (
                        "The current result still reports a confirmed matching finding; "
                        "the resolved decision conflicts with that evidence."
                    )
                elif candidate is not None and candidate.get("validation_status") == "needs_confirmation":
                    status = "unverified"
                    reason = (
                        "The current result still reports a matching candidate that needs confirmation; "
                        "the resolved decision cannot override it."
                    )
                else:
                    block_reason = _resolution_block_reason(
                        prior,
                        decision,
                        current_manifest,
                        previous_sources,
                        current_sources,
                        current,
                        aliases,
                    )
                    if block_reason:
                        status = "unverified"
                        reason = block_reason
        elif association is not None:
            candidate, validation_status = association
            status = "persisting" if validation_status == "confirmed" else "unverified"
            reason = (
                "The current semantic result reports the same root cause."
                if status == "persisting"
                else "The current semantic result only treats the same root cause as a candidate."
            )
            evidence = copy.deepcopy(candidate.get("evidence", []))
        else:
            status = "unverified"
            reason = (
                "The fallback association is ambiguous because multiple prior/current findings could match; "
                "so this finding remains unverified."
                if finding_id in ambiguous
                else "The current result did not provide an explicit resolution decision or matching finding."
            )
            evidence = []
        statuses.append({
            "previous_finding_id": finding_id,
            "current_finding_id": association[0].get("finding_id") if association is not None else None,
            "status": status,
            "reason": reason,
            "evidence": evidence,
        })

    new_findings = [
        item["finding_id"]
        for item in (current or {}).get("findings", [])
        if item.get("finding_id") not in matched_ids and item.get("validation_status") == "confirmed"
    ]
    coverage = (current or {}).get("coverage", [])
    coverage_gaps = [
        {
            "file": item.get("file"),
            "semantic_status": item.get("semantic_status"),
            "reason": item.get("reason", ""),
        }
        for item in coverage
        if item.get("semantic_status") != "completed"
    ]
    if not current:
        recheck_status = "partial"
    elif current.get("review_status") in {"failed", "partial", "uncovered"}:
        recheck_status = "partial"
    elif coverage_gaps or any(item["status"] == "unverified" for item in statuses):
        recheck_status = "partial"
    else:
        recheck_status = "completed"

    return {
        "schema_version": "2",
        "recheck_status": recheck_status,
        "previous_report_fingerprint": previous_report_fingerprint,
        "previous_input_fingerprint": previous["input_fingerprint"],
        "current_input_fingerprint": current_manifest["content_fingerprint"],
        "findings": statuses,
        "new_findings": new_findings,
        "coverage_gaps": coverage_gaps,
    }


def render_recheck(result: dict[str, Any]) -> str:
    """Render a compact human report from a validated recheck result."""
    lines = [
        "# Code Review Recheck",
        "",
        f"**Status:** {result.get('recheck_status', 'unknown')}",
        f"**Previous report:** `{result.get('previous_report_fingerprint', 'unknown')}`",
        f"**Current input:** `{result.get('current_input_fingerprint', 'unknown')}`",
        "",
        "## Previous findings",
        "",
    ]
    if not result.get("findings"):
        lines.append("No previous findings were supplied.")
    for item in result.get("findings", []):
        lines.extend([
            f"- `{item.get('previous_finding_id', '')}`: **{item.get('status', 'unknown')}** — {item.get('reason', '')}",
        ])
        for evidence in item.get("evidence", []):
            lines.append(
                f"  - {evidence.get('file', '')} ({evidence.get('side', '')}:"
                f"L{evidence.get('line_start', '?')}-L{evidence.get('line_end', '?')})"
            )
    lines.extend(["", "## New findings", ""])
    if result.get("new_findings"):
        lines.extend(f"- `{finding_id}`" for finding_id in result["new_findings"])
    else:
        lines.append("No new confirmed findings were recorded.")
    if result.get("coverage_gaps"):
        lines.extend(["", "## Coverage gaps", ""])
        for gap in result["coverage_gaps"]:
            lines.append(f"- `{gap.get('file', '')}`: {gap.get('semantic_status', '')} — {gap.get('reason', '')}")
    return "\n".join(lines) + "\n"
