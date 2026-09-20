"""Aggregator – deduplicates and arbitrates findings from multiple review agents.

Main entry point: ``Aggregator.aggregate(agent_results, pr_url, task_id)``

Steps:
  1. Deduplicate findings within ±3 lines / same category
  2. Arbitrate conflicting severity via weighted confidence
  3. Generate an executive summary with Claude
  4. Render the full Markdown report
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import anthropic
from pydantic import BaseModel

from agents.base import AgentResult, Finding

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-opus-4-6"

AGENT_WEIGHTS: Dict[str, float] = {
    "SecurityAgent":     1.0,
    "LogicAgent":        0.8,
    "PerformanceAgent":  0.6,
    "StyleAgent":        0.4,
}

_SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
_SEVERITY_ICONS = {
    "CRITICAL": "🔴",
    "HIGH":     "🟠",
    "MEDIUM":   "🟡",
    "LOW":      "🟢",
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class DeduplicatedFinding(BaseModel):
    """A finding after dedup and arbitration."""

    file: str
    line_start: int
    line_end: int
    severity: str
    category: str
    description: str
    suggestion: str
    confidence: float
    source_agents: List[str]


class AggregatedReport(BaseModel):
    task_id: Optional[int]
    pr_url: str
    findings: List[DeduplicatedFinding]
    executive_summary: str
    markdown_report: str
    stats: Dict[str, Any]
    pr_metadata: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class Aggregator:
    """Merges AgentResult objects into a single AggregatedReport."""

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def aggregate(
        self,
        agent_results: List[AgentResult],
        pr_url: str = "",
        task_id: int | None = None,
        pr_metadata: Dict[str, Any] | None = None,
    ) -> AggregatedReport:
        """Main entry point: dedup + arbitrate + generate report."""
        # Flatten all findings, tagging each with its agent name
        tagged: List[tuple[Finding, str]] = [
            (finding, result.agent_name)
            for result in agent_results
            for finding in result.findings
        ]

        deduped = self._deduplicate(tagged)

        # Sort: severity order first, then file, then line
        deduped.sort(key=lambda f: (
            _SEVERITY_ORDER.index(f.severity) if f.severity in _SEVERITY_ORDER else 99,
            f.file,
            f.line_start,
        ))

        stats = self._compute_stats(deduped, agent_results)
        executive_summary = self._generate_executive_summary(deduped)

        report = AggregatedReport(
            task_id=task_id,
            pr_url=pr_url,
            findings=deduped,
            executive_summary=executive_summary,
            markdown_report="",
            stats=stats,
            pr_metadata=pr_metadata or {},
        )
        report.markdown_report = self._render_markdown(report)
        return report

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _deduplicate(
        self,
        findings_with_agent: List[tuple[Finding, str]],
    ) -> List[DeduplicatedFinding]:
        """Merge findings that share (file, category) and are within ±3 lines."""
        # Group by (file, category)
        groups: Dict[tuple[str, str], List[tuple[Finding, str]]] = {}
        for finding, agent in findings_with_agent:
            key = (finding.file, finding.category)
            groups.setdefault(key, []).append((finding, agent))

        result: List[DeduplicatedFinding] = []
        for (_file, _category), items in groups.items():
            clusters = self._cluster_by_proximity(items)
            for cluster in clusters:
                result.append(self._merge_cluster(cluster))
        return result

    @staticmethod
    def _cluster_by_proximity(
        items: List[tuple[Finding, str]],
    ) -> List[List[tuple[Finding, str]]]:
        """Group items whose line_start values are within 3 lines of each other."""
        if not items:
            return []
        # Sort by line_start so we can do a single pass
        sorted_items = sorted(items, key=lambda x: x[0].line_start)
        clusters: List[List[tuple[Finding, str]]] = [[sorted_items[0]]]
        for item in sorted_items[1:]:
            last_cluster = clusters[-1]
            last_line = last_cluster[-1][0].line_start
            if abs(item[0].line_start - last_line) <= 3:
                last_cluster.append(item)
            else:
                clusters.append([item])
        return clusters

    def _merge_cluster(
        self,
        cluster: List[tuple[Finding, str]],
    ) -> DeduplicatedFinding:
        """Merge a cluster of related findings into one DeduplicatedFinding."""
        # Pick the finding with highest weighted confidence as "primary"
        def weighted_conf(item: tuple[Finding, str]) -> float:
            finding, agent = item
            weight = AGENT_WEIGHTS.get(agent, 0.5)
            return weight * finding.confidence

        primary_item = max(cluster, key=weighted_conf)
        primary, primary_agent = primary_item

        # Collect all agents in this cluster
        source_agents: List[str] = list({
            agent for _, agent in cluster
        })

        # Compute weighted average confidence
        total_weight = sum(AGENT_WEIGHTS.get(a, 0.5) for _, a in cluster)
        weighted_confidence = (
            sum(AGENT_WEIGHTS.get(a, 0.5) * f.confidence for f, a in cluster)
            / total_weight
        )

        # Pick the longer description / suggestion
        description = max(
            (f.description for f, _ in cluster), key=len
        )
        suggestion = max(
            (f.suggestion for f, _ in cluster), key=len
        )

        # Determine severity
        severity = self._arbitrate_severity(
            cluster, weighted_confidence, primary_agent
        )

        return DeduplicatedFinding(
            file=primary.file,
            line_start=primary.line_start,
            line_end=primary.line_end,
            severity=severity,
            category=primary.category,
            description=description,
            suggestion=suggestion,
            confidence=round(weighted_confidence, 4),
            source_agents=sorted(source_agents),
        )

    @staticmethod
    def _arbitrate_severity(
        cluster: List[tuple[Finding, str]],
        weighted_confidence: float,
        primary_agent: str,
    ) -> str:
        """Decide final severity; SecurityAgent CRITICAL is never downgraded."""
        # Guard: SecurityAgent CRITICAL is immutable
        for finding, agent in cluster:
            if agent == "SecurityAgent" and finding.severity == "CRITICAL":
                return "CRITICAL"

        # Derive from weighted confidence
        if weighted_confidence >= 0.85:
            return "CRITICAL"
        if weighted_confidence >= 0.65:
            return "HIGH"
        if weighted_confidence >= 0.40:
            return "MEDIUM"
        return "LOW"

    # ------------------------------------------------------------------
    # Executive summary (Claude)
    # ------------------------------------------------------------------

    def _generate_executive_summary(self, findings: List[DeduplicatedFinding]) -> str:
        """Call Claude to produce a 3-5 sentence executive summary."""
        if not findings:
            return "No issues were found. The code looks clean across all review dimensions."

        counts = _count_by_severity(findings)
        bullet_lines = [
            f"- {sev}: {counts.get(sev, 0)}"
            for sev in _SEVERITY_ORDER
            if counts.get(sev, 0) > 0
        ]
        counts_text = "\n".join(bullet_lines)

        categories = list({f.category for f in findings})
        sample_descriptions = "\n".join(
            f"- [{f.severity}] {f.description}" for f in findings[:5]
        )

        prompt = (
            "You are a senior engineering lead summarising a code review.\n"
            "Write a concise executive summary of 3-5 sentences for the findings below.\n"
            "Focus on the most critical issues, patterns observed, and overall risk level.\n"
            "Do not list every issue — give a high-level picture.\n\n"
            f"Total findings: {len(findings)}\n"
            f"By severity:\n{counts_text}\n"
            f"Categories found: {', '.join(categories)}\n"
            f"Sample findings:\n{sample_descriptions}"
        )

        try:
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception:  # noqa: BLE001
            # Fallback: generate a plain-text summary without Claude
            total = len(findings)
            parts = [
                f"{sev}: {counts[sev]}" for sev in _SEVERITY_ORDER if counts.get(sev)
            ]
            return (
                f"Code review completed with {total} finding(s) "
                f"({', '.join(parts)}). "
                "Please review the detailed findings below."
            )

    # ------------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------------

    def _render_markdown(self, report: AggregatedReport) -> str:
        """Render the full Markdown report string."""
        lines: List[str] = []
        lines.append("# Code Review Report\n")

        meta = report.pr_metadata
        if meta:
            author = meta.get("author", "")
            branch = meta.get("head_branch", "")
            title = meta.get("title", "")
            if title:
                lines.append(f"**PR:** {title}")
            if author:
                lines.append(f"**Author:** {author}")
            if branch:
                lines.append(f"**Branch:** {branch}")
            if meta.get("head_sha"):
                lines.append(f"**Reviewed commit:** `{meta['head_sha']}`")
            if meta.get("base_sha"):
                lines.append(f"**Base commit:** `{meta['base_sha']}`")
            if meta.get("skipped_files"):
                lines.append("\n**Coverage:** The following files were not analyzed:")
                for filename, reason in meta["skipped_files"].items():
                    lines.append(f"- `{filename}`: {reason}")
            lines.append("")

        lines.append("## Executive Summary")
        lines.append(report.executive_summary)
        lines.append("")

        # Statistics table
        lines.append("## Statistics")
        lines.append("| Severity | Count |")
        lines.append("|----------|-------|")
        counts = _count_by_severity(report.findings)
        for sev in _SEVERITY_ORDER:
            lines.append(f"| {sev} | {counts.get(sev, 0)} |")
        lines.append("")

        # Findings by severity
        lines.append("## Findings\n")
        findings_by_sev: Dict[str, List[DeduplicatedFinding]] = {}
        for f in report.findings:
            findings_by_sev.setdefault(f.severity, []).append(f)

        for sev in _SEVERITY_ORDER:
            sev_findings = findings_by_sev.get(sev, [])
            icon = _SEVERITY_ICONS.get(sev, "")
            lines.append(f"### {icon} {sev}")
            if not sev_findings:
                lines.append("_No issues._\n")
                continue
            for f in sev_findings:
                lines.append(
                    f"#### [{f.category}] `{f.file}` "
                    f"L{f.line_start}-{f.line_end}"
                )
                lines.append(f"**Description:** {f.description}  ")
                lines.append(f"**Suggestion:** {f.suggestion}  ")
                sources = ", ".join(f.source_agents)
                lines.append(
                    f"**Confidence:** {f.confidence:.0%} | **Sources:** {sources}"
                )
                lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Stats helper
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_stats(
        findings: List[DeduplicatedFinding],
        agent_results: List[AgentResult],
    ) -> Dict[str, Any]:
        counts = _count_by_severity(findings)
        by_agent = {
            r.agent_name: len(r.findings) for r in agent_results
        }
        return {
            "total": len(findings),
            "by_severity": counts,
            "by_agent": by_agent,
        }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _count_by_severity(findings: List[DeduplicatedFinding]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
