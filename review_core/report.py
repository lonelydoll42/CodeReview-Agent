"""Deterministic Markdown rendering for validated version 2 results."""

from __future__ import annotations

from typing import Any


_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _code(value: Any) -> str:
    return "`" + str(value).replace("`", "\\`") + "`"


def _unknown(value: Any) -> str:
    return "unknown" if value is None else str(value)


def render_review(result: dict[str, Any]) -> str:
    """Render a v2 result without model calls or nondeterministic timestamps."""
    if result.get("schema_version") != "2":
        raise ValueError("result.schema_version must be '2'")
    status = result.get("review_status", "failed")
    reported_findings = result.get("findings", [])
    findings = sorted(
        [item for item in reported_findings if item.get("validation_status") == "confirmed"],
        key=lambda item: (
            _SEVERITY_ORDER.get(str(item.get("severity", "")).upper(), 99),
            str(item.get("category", "")),
            str(item.get("finding_id", "")),
        ),
    )
    candidates = sorted(
        [item for item in reported_findings if item.get("validation_status") != "confirmed"],
        key=lambda item: (
            _SEVERITY_ORDER.get(str(item.get("severity", "")).upper(), 99),
            str(item.get("category", "")),
            str(item.get("finding_id", "")),
        ),
    )
    measurements = result.get("measurements", {})
    lines = [
        "# Code Review Report",
        "",
        f"**Status:** {_cell(status)}",
        f"**Input fingerprint:** {_code(result.get('input_fingerprint', 'unknown'))}",
        f"**Rules version:** {_cell(result.get('rules_version', 'unknown'))}",
        "",
        "## Execution",
        "",
        f"- Host: {_cell(_unknown(measurements.get('host')))}",
        f"- Model: {_cell(_unknown(measurements.get('model')))}",
        f"- Tokens: {_cell(_unknown(measurements.get('tokens')))}",
        f"- Cost: {_cell(_unknown(measurements.get('cost')))}",
        "",
        "## Findings",
        "",
    ]
    if not findings and not candidates:
        if status == "completed":
            lines.append("No confirmed findings were reported for the selected files.")
        elif status == "uncovered":
            lines.append("No selected file received semantic review coverage.")
        elif status == "failed":
            lines.append("The review failed; no conclusion can be drawn from an empty finding list.")
        else:
            lines.append("No findings were reported, but the review was incomplete.")
        lines.append("")
    else:
        if findings:
            lines.extend(_render_findings(findings, title_prefix="", start=1))
        else:
            lines.append("No confirmed findings were reported.")
            lines.append("")
    if candidates:
        lines.extend(["## Needs Confirmation", ""])
        lines.extend(_render_findings(candidates, title_prefix="Candidate ", start=1))

    coverage = sorted(result.get("coverage", []), key=lambda item: str(item.get("file", "")))
    lines.extend(["## Coverage", ""])
    if not coverage:
        lines.extend(["No changed files were selected for coverage.", ""])
    else:
        lines.extend(
            [
                "| File | Semantic | Static | Reason | Context gaps |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in coverage:
            gaps = ", ".join(item.get("context_gaps", []))
            lines.append(
                f"| {_cell(item.get('file', ''))} | {_cell(item.get('semantic_status', ''))} "
                f"| {_cell(item.get('static_status', ''))} | {_cell(item.get('reason', ''))} "
                f"| {_cell(gaps)} |"
            )
        lines.append("")

    tool_runs = result.get("tool_runs", [])
    lines.extend(["## Tool Runs", ""])
    if tool_runs:
        lines.extend(["| Tool | Status | Details |", "| --- | --- | --- |"])
        for item in tool_runs:
            details = item.get("error") or item.get("reason") or item.get("version") or ""
            lines.append(
                f"| {_cell(item.get('tool', ''))} | {_cell(item.get('status', ''))} "
                f"| {_cell(details)} |"
            )
    else:
        lines.append("No tool run was recorded.")
    lines.append("")

    static_candidates = [
        (run, candidate)
        for run in tool_runs
        for candidate in run.get("findings", [])
    ]
    if static_candidates:
        lines.extend(["## Static Candidates", "", "These tool signals are unconfirmed and are not counted as findings.", ""])
        for run, candidate in static_candidates:
            location = f"{candidate.get('file', '')}:L{candidate.get('line', '?')}"
            rule = candidate.get("rule_id", "unknown")
            message = candidate.get("message", "")
            lines.append(
                f"- {_code(location)} [{_cell(run.get('tool', 'tool'))}/"
                f"{_cell(rule)}] {_cell(message)}"
            )
        lines.append("")

    limits = result.get("limits", {})
    lines.extend(["## Limits", ""])
    if limits:
        for key in sorted(limits, key=str):
            lines.append(f"- {_cell(key)}: {_cell(limits[key])}")
    else:
        lines.append("No explicit limits were recorded.")
    lines.append("")
    return "\n".join(lines)


def _render_findings(findings: list[dict[str, Any]], *, title_prefix: str, start: int) -> list[str]:
    lines: list[str] = []
    for index, finding in enumerate(findings, start=start):
        title = f"{title_prefix}{index}. [{_cell(finding.get('severity', ''))}] "
        title += _cell(finding.get("category", ""))
        lines.extend(
            [
                f"### {title}",
                "",
                f"**Validation:** {_cell(finding.get('validation_status', 'unknown'))}",
                f"**Root cause:** {_cell(finding.get('root_cause', ''))}",
                f"**Trigger:** {_cell(finding.get('trigger', ''))}",
                f"**Impact:** {_cell(finding.get('impact', ''))}",
                f"**Change attribution:** {_cell(finding.get('change_reason', ''))}",
                f"**Suggestion:** {_cell(finding.get('suggestion', ''))}",
                "**Evidence:**",
            ]
        )
        for evidence in finding.get("evidence", []):
            location = (
                f"{evidence.get('file', '')} ({evidence.get('side', '')}:"
                f"L{evidence.get('line_start', '?')}-L{evidence.get('line_end', '?')})"
            )
            lines.append(
                f"- {_code(location)}; snapshot {_code(evidence.get('snapshot_fingerprint', ''))}: "
                f"{_cell(evidence.get('description', ''))}"
            )
        sources = finding.get("sources", [])
        lines.append(f"**Sources:** {_cell(', '.join(sources)) if sources else 'unknown'}")
        lines.append("")
    return lines
