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
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if not isinstance(item, dict):
            return False
        file = item.get("file")
        side = item.get("side")
        start = item.get("line_start")
        end = item.get("line_end")
        if (
            not isinstance(file, str)
            or not isinstance(side, str)
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
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
        if item.get("snapshot_fingerprint") != snapshot.get("fingerprint"):
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
        if status not in _STATUSES:
            raise ReviewValidationError(f"result.recheck_decisions[{index}].status is invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise ReviewValidationError(f"result.recheck_decisions[{index}].reason is required")
        evidence = item.get("evidence", [])
        if evidence and not _evidence_valid(evidence, sources):
            raise ReviewValidationError(f"result.recheck_decisions[{index}].evidence is invalid")
        decisions[finding_id] = {
            "previous_finding_id": finding_id,
            "status": status,
            "reason": reason,
            "evidence": copy.deepcopy(evidence),
        }
    return decisions


def _associate(
    previous: dict[str, Any],
    current: dict[str, Any],
    previous_manifest: dict[str, Any],
    current_manifest: dict[str, Any],
) -> dict[str, tuple[dict[str, Any], str]]:
    aliases = _path_aliases(previous_manifest, current_manifest)
    current_by_id = {
        item.get("finding_id"): item
        for item in current.get("findings", [])
        if isinstance(item.get("finding_id"), str)
    }
    unused = list(current.get("findings", []))
    associated: dict[str, tuple[dict[str, Any], str]] = {}
    for prior in previous.get("findings", []):
        prior_id = prior["finding_id"]
        candidate = current_by_id.get(prior_id)
        if candidate is None:
            same = [item for item in unused if _semantic_key(item) == _semantic_key(prior)]
            prior_paths = _evidence_paths(prior, aliases)
            if prior_paths:
                same_with_path = [
                    item for item in same
                    if _evidence_paths(item, aliases) & prior_paths
                ]
                if len(same_with_path) == 1:
                    candidate = same_with_path[0]
            if candidate is None and len(same) == 1:
                candidate = same[0]
        if candidate is not None:
            associated[prior_id] = (candidate, candidate.get("validation_status", "needs_confirmation"))
            if candidate in unused:
                unused.remove(candidate)
    return associated


def recheck_review(
    previous_result: dict[str, Any],
    previous_manifest: dict[str, Any],
    current_manifest: dict[str, Any],
    current_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare a prior validated result with a new snapshot and result.

    A missing current semantic result never becomes ``resolved``.  Resolution
    requires an explicit host decision with a reason describing how the trigger
    changed; this keeps a clean/empty response from silently proving a fix.
    """
    previous_manifest = validate_manifest(previous_manifest)
    current_manifest = validate_manifest(current_manifest)
    previous = validate_review(previous_result, previous_manifest)
    current: dict[str, Any] | None = None
    if current_result is not None:
        current = validate_review(current_result, current_manifest)

    previous_report_fingerprint = _fingerprint(previous)
    aliases = _path_aliases(previous, current_manifest)
    current_sources = _manifest_sources(current_manifest)
    previous_ids = {item["finding_id"] for item in previous.get("findings", [])}
    decisions = _decision_map(current or {}, current_sources, previous_ids)
    associated = _associate(previous, current or {"findings": []}, previous_manifest, current_manifest)
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
                "The current result did not provide an explicit resolution decision or matching finding."
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
