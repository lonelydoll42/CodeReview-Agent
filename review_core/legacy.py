"""Dependency-free helpers that preserve the pre-v2 service report behavior."""

from __future__ import annotations

from typing import Any


AGENT_WEIGHTS: dict[str, float] = {
    "SecurityAgent": 1.0,
    "LogicAgent": 0.8,
    "PerformanceAgent": 0.6,
    "StyleAgent": 0.4,
}
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
SEVERITY_ICONS = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🟢",
}


def _overlap(left: Any, right: Any) -> bool:
    return left.line_start <= right.line_end and right.line_start <= left.line_end


def cluster_by_proximity(items: list[tuple[Any, str]]) -> list[list[tuple[Any, str]]]:
    if not items:
        return []
    sorted_items = sorted(items, key=lambda item: (item[0].line_start, item[0].line_end))
    clusters: list[list[tuple[Any, str]]] = [[sorted_items[0]]]
    for item in sorted_items[1:]:
        matching = next(
            (
                cluster
                for cluster in clusters
                if item[1] not in {agent for _, agent in cluster}
                and all(_overlap(item[0], existing[0]) for existing in cluster)
            ),
            None,
        )
        if matching is None:
            clusters.append([item])
        else:
            matching.append(item)
    return clusters


def arbitrate_severity(cluster: list[tuple[Any, str]]) -> str:
    for finding, agent in cluster:
        if agent == "SecurityAgent" and finding.severity == "CRITICAL":
            return "CRITICAL"
    votes: dict[str, float] = {}
    for finding, agent in cluster:
        votes[finding.severity] = votes.get(finding.severity, 0.0) + (
            AGENT_WEIGHTS.get(agent, 0.5) * finding.confidence
        )
    return min(
        votes,
        key=lambda severity: (
            -votes[severity],
            SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else 99,
            severity,
        ),
    )


def merge_legacy_cluster(cluster: list[tuple[Any, str]]) -> dict[str, Any]:
    def weighted_conf(item: tuple[Any, str]) -> float:
        finding, agent = item
        return AGENT_WEIGHTS.get(agent, 0.5) * finding.confidence

    primary, _ = max(cluster, key=weighted_conf)
    source_agents = sorted({agent for _, agent in cluster})
    total_weight = sum(AGENT_WEIGHTS.get(agent, 0.5) for _, agent in cluster)
    confidence = sum(
        AGENT_WEIGHTS.get(agent, 0.5) * finding.confidence
        for finding, agent in cluster
    ) / total_weight
    return {
        "file": primary.file,
        "line_start": primary.line_start,
        "line_end": primary.line_end,
        "severity": arbitrate_severity(cluster),
        "category": primary.category,
        "description": primary.description,
        "suggestion": primary.suggestion,
        "confidence": round(confidence, 4),
        "source_agents": source_agents,
    }


def deduplicate_legacy(findings_with_agent: list[tuple[Any, str]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[tuple[Any, str]]] = {}
    for finding, agent in findings_with_agent:
        groups.setdefault((finding.file, finding.category), []).append((finding, agent))
    merged = [
        merge_legacy_cluster(cluster)
        for items in groups.values()
        for cluster in cluster_by_proximity(items)
    ]
    merged.sort(
        key=lambda finding: (
            SEVERITY_ORDER.index(finding["severity"])
            if finding["severity"] in SEVERITY_ORDER
            else 99,
            finding["file"],
            finding["line_start"],
        )
    )
    return merged


def count_by_severity(findings: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        severity = finding.severity if hasattr(finding, "severity") else finding["severity"]
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def coverage_warning(review_status: str, coverage: list[Any]) -> str:
    skipped = [item for item in coverage if item.status in {"unsupported", "skipped"}]
    partial = [item for item in coverage if item.status in {"partial", "failed"}]
    if review_status == "uncovered":
        return "No supported files were available, so this report does not cover the changed code."
    if partial:
        return "Some file analyses did not complete, so this report is scoped to the successful agent runs."
    if skipped:
        return f"{len(skipped)} changed file(s) were not analyzed, so this is not a conclusion about the entire change."
    return ""


def fallback_summary(findings: list[Any]) -> str:
    total = len(findings)
    counts = count_by_severity(findings)
    parts = [f"{severity}: {counts[severity]}" for severity in SEVERITY_ORDER if counts.get(severity)]
    return (
        f"Code review completed with {total} finding(s) ({', '.join(parts)}). "
        "Please review the detailed findings below."
    )


def render_legacy_markdown(report: Any) -> str:
    lines: list[str] = ["# Code Review Report\n"]
    metadata = report.pr_metadata
    if metadata:
        title = metadata.get("title", "")
        if title:
            lines.append(f"**PR:** {title}")
        if metadata.get("author"):
            lines.append(f"**Author:** {metadata['author']}")
        if metadata.get("head_branch"):
            lines.append(f"**Branch:** {metadata['head_branch']}")
        if metadata.get("head_sha"):
            lines.append(f"**Reviewed commit:** `{metadata['head_sha']}`")
        if metadata.get("base_sha"):
            lines.append(f"**Base commit:** `{metadata['base_sha']}`")
        if metadata.get("skipped_files"):
            lines.append("\n**Coverage:** The following files were not analyzed:")
            for filename, reason in metadata["skipped_files"].items():
                lines.append(f"- `{filename}`: {reason}")
        lines.append("")

    if report.coverage:
        lines.append("**Analysis coverage:**")
        for item in report.coverage:
            details = f"{item.completed_agents}/{item.expected_agents} agents"
            reason = f" ({item.reason})" if item.reason else ""
            lines.append(f"- `{item.filename}`: {item.status}, {details}{reason}")
        lines.append("")

    lines.extend(["## Executive Summary", report.executive_summary, ""])
    lines.extend(["## Statistics", "| Severity | Count |", "|----------|-------|"])
    counts = count_by_severity(report.findings)
    for severity in SEVERITY_ORDER:
        lines.append(f"| {severity} | {counts.get(severity, 0)} |")
    lines.extend(["", "## Findings\n"])

    findings_by_severity: dict[str, list[Any]] = {}
    for finding in report.findings:
        findings_by_severity.setdefault(finding.severity, []).append(finding)
    for severity in SEVERITY_ORDER:
        items = findings_by_severity.get(severity, [])
        lines.append(f"### {SEVERITY_ICONS.get(severity, '')} {severity}")
        if not items:
            lines.append("_No issues._\n")
            continue
        for finding in items:
            lines.append(
                f"#### [{finding.category}] `{finding.file}` "
                f"L{finding.line_start}-{finding.line_end}"
            )
            lines.append(f"**Description:** {finding.description}  ")
            lines.append(f"**Suggestion:** {finding.suggestion}  ")
            lines.append(
                f"**Confidence:** {finding.confidence:.0%} | "
                f"**Sources:** {', '.join(finding.source_agents)}"
            )
            lines.append("")
    return "\n".join(lines)
