"""Aggregator – deduplicates and arbitrates findings from multiple review agents.

Main entry point: ``Aggregator.aggregate(agent_results, pr_url, task_id)``

Steps:
  1. Deduplicate corroborating findings with overlapping line evidence
  2. Arbitrate conflicting severity via weighted confidence
  3. Generate an executive summary with Claude
  4. Render the full Markdown report
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agents.base import AgentExecution, AgentResult, FileCoverage, Finding
from review_core.legacy import (
    AGENT_WEIGHTS,
    SEVERITY_ORDER as _SEVERITY_ORDER,
    arbitrate_severity,
    cluster_by_proximity,
    count_by_severity as _legacy_count_by_severity,
    coverage_warning as _legacy_coverage_warning,
    deduplicate_legacy,
    fallback_summary,
    merge_legacy_cluster,
    render_legacy_markdown,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-opus-4-6"


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
        if enable_llm_summary:
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
            )
        else:
            self._client = None

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
        """Run the dependency-free legacy deduplication behavior."""
        return [
            DeduplicatedFinding(**item)
            for item in deduplicate_legacy(findings_with_agent)
        ]

    @staticmethod
    def _cluster_by_proximity(
        items: List[tuple[Finding, str]],
    ) -> List[List[tuple[Finding, str]]]:
        """Group only corroborating findings with overlapping evidence.

        A proximity threshold is too permissive for independent findings from
        one agent, and it also allows transitive clusters.  A cluster may
        contain at most one finding per agent, and every finding must overlap
        every other finding in the cluster's reported line range.
        """
        return cluster_by_proximity(items)

    @staticmethod
    def _line_ranges_overlap(left: Finding, right: Finding) -> bool:
        """Return whether two findings point to at least one common line."""
        return left.line_start <= right.line_end and right.line_start <= left.line_end

    def _merge_cluster(
        self,
        cluster: List[tuple[Finding, str]],
    ) -> DeduplicatedFinding:
        return DeduplicatedFinding(**merge_legacy_cluster(cluster))

    @staticmethod
    def _arbitrate_severity(
        cluster: List[tuple[Finding, str]],
    ) -> str:
        return arbitrate_severity(cluster)

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
        return fallback_summary(findings)

    # ------------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------------

    def _render_markdown(self, report: AggregatedReport) -> str:
        return render_legacy_markdown(report)

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
    return _legacy_count_by_severity(findings)


def _coverage_warning(review_status: str, coverage: List[FileCoverage]) -> str:
    return _legacy_coverage_warning(review_status, coverage)
