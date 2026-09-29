"""Aggregator – deduplicates and arbitrates findings from multiple review agents.

Main entry point: ``Aggregator.aggregate(agent_results, pr_url, task_id)``

Steps:
  1. Deduplicate findings within ±3 lines / same category
  2. Arbitrate conflicting severity via weighted confidence
  3. Generate an executive summary with Claude
  4. Render the full Markdown report
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import anthropic
from pydantic import BaseModel, Field

from agents.base import AgentExecution, AgentResult, FileCoverage, Finding

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
    pr_metadata: Dict[str, Any] = Field(default_factory=dict)
    review_status: str = "completed"
    coverage: List[FileCoverage] = Field(default_factory=list)
    agent_executions: List[AgentExecution] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class Aggregator:
    """Merges AgentResult objects into a single AggregatedReport."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        enable_llm_summary: bool = True,
    ) -> None:
        """Create an aggregator.

        ``enable_llm_summary=False`` is used by the service-free local
        diff entry point.  It keeps report formatting and deduplication shared
        with the main workflow while guaranteeing that aggregation does not
        make an Anthropic request.
        """
        self._enable_llm_summary = enable_llm_summary
        # A local/offline aggregation must not require an API key or even
        # construct a provider client.  The deterministic path is also used
        # when a provider is unavailable at runtime.
        self._client = (
            anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
            if enable_llm_summary
            else None
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
        *,
        review_status: str = "completed",
        coverage: List[FileCoverage] | None = None,
        agent_executions: List[AgentExecution] | None = None,
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

        coverage_records = coverage or []
        execution_records = agent_executions or []
        stats = self._compute_stats(
            deduped,
            agent_results,
            review_status=review_status,
            coverage=coverage_records,
            agent_executions=execution_records,
        )
        executive_summary = self._generate_executive_summary(
            deduped,
            review_status=review_status,
            coverage=coverage_records,
        )

        report = AggregatedReport(
            task_id=task_id,
            pr_url=pr_url,
            findings=deduped,
            executive_summary=executive_summary,
            markdown_report="",
            stats=stats,
            pr_metadata=pr_metadata or {},
            review_status=review_status,
            coverage=coverage_records,
            agent_executions=execution_records,
        )
        report.markdown_report = self._render_markdown(report)
        return report

    async def aggregate_async(self, *args: Any, **kwargs: Any) -> AggregatedReport:
        """Run the legacy synchronous aggregation off the event loop.

        The provider summary client is intentionally kept synchronous for
        backwards compatibility with callers and tests.  Production review
        orchestration uses this method so that a slow summary request cannot
        block unrelated API requests or agent tasks.
        """
        return await asyncio.to_thread(self.aggregate, *args, **kwargs)

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
        primary, _ = primary_item

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
        severity = self._arbitrate_severity(cluster)

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
    ) -> str:
        """Vote on reported severity; preserve SecurityAgent CRITICAL findings."""
        # Guard: SecurityAgent CRITICAL is immutable
        for finding, agent in cluster:
            if agent == "SecurityAgent" and finding.severity == "CRITICAL":
                return "CRITICAL"

        votes: Dict[str, float] = {}
        for finding, agent in cluster:
            votes[finding.severity] = (
                votes.get(finding.severity, 0.0)
                + AGENT_WEIGHTS.get(agent, 0.5) * finding.confidence
            )

        # Confidence measures certainty, not impact. Only select a reported
        # severity, breaking equal votes in favour of the higher known risk.
        return min(votes, key=lambda severity: (
            -votes[severity],
            _SEVERITY_ORDER.index(severity) if severity in _SEVERITY_ORDER else 99,
            severity,
        ))

    # ------------------------------------------------------------------
    # Executive summary (Claude)
    # ------------------------------------------------------------------

    def _generate_executive_summary(
        self,
        findings: List[DeduplicatedFinding],
        *,
        review_status: str = "completed",
        coverage: List[FileCoverage] | None = None,
    ) -> str:
        """Call Claude to produce a 3-5 sentence executive summary."""
        coverage_warning = _coverage_warning(review_status, coverage or [])
        if not findings:
            if review_status == "failed":
                return "The review could not complete because all agent analyses failed; no conclusion can be drawn from this report."
            if review_status == "partial":
                return "The review was incomplete because one or more agent analyses failed or timed out; the findings below cover only successful agent runs."
            if coverage_warning:
                return f"No issues were found in the analyzed files. {coverage_warning}"
            return "No issues were found. The code looks clean across all review dimensions."

        if not self._enable_llm_summary or self._client is None:
            summary = self._fallback_executive_summary(findings)
            return f"{summary} {coverage_warning}" if coverage_warning else summary

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
            summary = response.content[0].text.strip()
            return f"{summary} {coverage_warning}" if coverage_warning else summary
        except Exception:  # noqa: BLE001
            # Fallback: generate a plain-text summary without Claude
            summary = self._fallback_executive_summary(findings)
            return f"{summary} {coverage_warning}" if coverage_warning else summary

    @staticmethod
    def _fallback_executive_summary(findings: List[DeduplicatedFinding]) -> str:
        """Build a deterministic summary when model output is disabled/unavailable."""
        total = len(findings)
        counts = _count_by_severity(findings)
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

        if report.coverage:
            lines.append("**Analysis coverage:**")
            for item in report.coverage:
                details = f"{item.completed_agents}/{item.expected_agents} agents"
                reason = f" ({item.reason})" if item.reason else ""
                lines.append(
                    f"- `{item.filename}`: {item.status}, {details}{reason}"
                )
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
        *,
        review_status: str = "completed",
        coverage: List[FileCoverage] | None = None,
        agent_executions: List[AgentExecution] | None = None,
    ) -> Dict[str, Any]:
        counts = _count_by_severity(findings)
        by_agent: Dict[str, int] = {}
        for result in agent_results:
            by_agent[result.agent_name] = by_agent.get(result.agent_name, 0) + len(result.findings)
        coverage_records = coverage or []
        execution_records = agent_executions or []
        return {
            "total": len(findings),
            "by_severity": counts,
            "by_agent": by_agent,
            "review_status": review_status,
            "coverage_complete": all(
                item.status == "covered" for item in coverage_records
            ) if coverage_records else review_status == "completed",
            "coverage": [item.model_dump() for item in coverage_records],
            "agent_failures": [
                item.model_dump()
                for item in execution_records
                if item.status in {"failed", "timeout"}
            ],
        }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _count_by_severity(findings: List[DeduplicatedFinding]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _coverage_warning(review_status: str, coverage: List[FileCoverage]) -> str:
    """Return a user-facing caveat whenever a report is not globally clean."""
    skipped = [item for item in coverage if item.status in {"unsupported", "skipped"}]
    partial = [item for item in coverage if item.status in {"partial", "failed"}]
    if review_status == "uncovered":
        return "No supported files were available, so this report does not cover the changed code."
    if partial:
        return "Some file analyses did not complete, so this report is scoped to the successful agent runs."
    if skipped:
        return f"{len(skipped)} changed file(s) were not analyzed, so this is not a conclusion about the entire change."
    return ""
