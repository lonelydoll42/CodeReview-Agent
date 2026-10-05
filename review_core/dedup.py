"""Conservative, deterministic deduplication for version 2 findings."""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any


_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _canonical_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().casefold()


def _identity(finding: dict[str, Any]) -> tuple[str, str, str, str] | None:
    category = _canonical_text(finding.get("category"))
    root_cause = _canonical_text(finding.get("root_cause"))
    trigger = _canonical_text(finding.get("trigger"))
    impact = _canonical_text(finding.get("impact"))
    if not all((category, root_cause, trigger, impact)):
        return None
    return category, root_cause, trigger, impact


def _primary_key(finding: dict[str, Any]) -> tuple[int, int, str]:
    confirmed = finding.get("validation_status") == "confirmed"
    severity = str(finding.get("severity", "")).upper()
    return (
        0 if confirmed else 1,
        -_SEVERITY_RANK.get(severity, 0),
        str(finding.get("finding_id", "")),
    )


def _evidence_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return any(
        left_evidence.get("file") == right_evidence.get("file")
        and left_evidence.get("side") == right_evidence.get("side")
        and left_evidence.get("snapshot_fingerprint")
        == right_evidence.get("snapshot_fingerprint")
        and left_evidence.get("line_start", 0) <= right_evidence.get("line_end", 0)
        and right_evidence.get("line_start", 0) <= left_evidence.get("line_end", 0)
        for left_evidence in left.get("evidence", [])
        for right_evidence in right.get("evidence", [])
    )


def _clusters(group: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for finding in sorted(group, key=lambda item: str(item.get("finding_id", ""))):
        target = next(
            (
                cluster
                for cluster in clusters
                if all(_evidence_overlaps(finding, existing) for existing in cluster)
            ),
            None,
        )
        if target is None:
            clusters.append([finding])
        else:
            target.append(finding)
    return clusters


def _merge(group: list[dict[str, Any]]) -> dict[str, Any]:
    group.sort(key=_primary_key)
    primary = group[0]
    if len(group) == 1:
        return primary

    evidence_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in group:
        for evidence in finding.get("evidence", []):
            key = (
                evidence.get("file"),
                evidence.get("side"),
                evidence.get("line_start"),
                evidence.get("line_end"),
                evidence.get("snapshot_fingerprint"),
                evidence.get("description"),
            )
            evidence_by_key[key] = copy.deepcopy(evidence)

    source_values = {
        source
        for finding in group
        for source in finding.get("sources", [])
        if isinstance(source, str)
    }
    original_ids = sorted(
        {
            finding["finding_id"]
            for finding in group
            if isinstance(finding.get("finding_id"), str)
        }
    )
    highest_severity = max(
        (finding.get("severity", "LOW").upper() for finding in group),
        key=lambda severity: _SEVERITY_RANK.get(severity, 0),
    )
    primary["severity"] = highest_severity
    primary["validation_status"] = (
        "confirmed"
        if any(finding.get("validation_status") == "confirmed" for finding in group)
        else "needs_confirmation"
    )
    primary["evidence"] = [
        evidence_by_key[key]
        for key in sorted(evidence_by_key, key=lambda value: tuple(map(str, value)))
    ]
    primary["sources"] = sorted(source_values)
    primary["finding_id"] = original_ids[0] if original_ids else primary.get("finding_id")
    primary["merged_finding_ids"] = original_ids
    return primary


def deduplicate_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge only findings with the same cause, trigger, and impact.

    A pair must share a captured source side and overlapping line range, and
    each cluster must be pairwise overlapping to avoid transitive merges.
    Findings without all semantic identity fields remain separate.
    """
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    independent: list[dict[str, Any]] = []
    for item in findings:
        identity = _identity(item)
        if identity is None:
            independent.append(copy.deepcopy(item))
        else:
            groups[identity].append(copy.deepcopy(item))

    merged = list(independent)
    for group in groups.values():
        merged.extend(_merge(cluster) for cluster in _clusters(group))

    merged.sort(
        key=lambda finding: (
            -_SEVERITY_RANK.get(str(finding.get("severity", "")).upper(), 0),
            str(finding.get("finding_id", "")),
        )
    )
    return merged
