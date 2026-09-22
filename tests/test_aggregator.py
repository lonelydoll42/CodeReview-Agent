"""Tests for Aggregator (Module C).

Coverage:
  1. Findings from two agents at the same location / category → merged to 1
  2. SecurityAgent CRITICAL is not downgraded by StyleAgent
  3. Weighted confidence calculation is correct
  4. Output Markdown contains all four severity sections
  5. Empty input → no findings, clean report
  6. Stats dict has expected keys
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.aggregator import AGENT_WEIGHTS, Aggregator, DeduplicatedFinding
from agents.base import AgentResult, FileDiff, Finding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(
    file: str = "foo.py",
    line_start: int = 10,
    line_end: int = 10,
    severity: str = "MEDIUM",
    category: str = "naming",
    description: str = "Bad name",
    suggestion: str = "Fix it",
    confidence: float = 0.8,
) -> Finding:
    return Finding(
        file=file,
        line_start=line_start,
        line_end=line_end,
        severity=severity,
        category=category,
        description=description,
        suggestion=suggestion,
        confidence=confidence,
    )


def _agent_result(
    agent_name: str,
    findings: list[Finding],
    summary: str = "ok",
) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        findings=findings,
        summary=summary,
        execution_time=0.1,
        token_used=100,
    )


def _make_summary_response(text: str = "All looks good.") -> MagicMock:
    """Mock anthropic Message for executive-summary calls."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _aggregator_with_mock_summary(summary_text: str = "Summary.") -> tuple[Aggregator, MagicMock]:
    """Return an Aggregator whose Claude client is mocked."""
    agg = Aggregator(api_key="test-key")
    mock_create = MagicMock(return_value=_make_summary_response(summary_text))
    agg._client.messages.create = mock_create
    return agg, mock_create


# ---------------------------------------------------------------------------
# Test 1 – dedup: same file, category, location → single merged finding
# ---------------------------------------------------------------------------

def test_dedup_same_location_same_category():
    """Two agents flagging the same spot / category must produce exactly 1 finding."""
    f1 = _finding(file="app.py", line_start=10, category="naming",
                  description="Short name", suggestion="Use longer name", confidence=0.9)
    f2 = _finding(file="app.py", line_start=11, category="naming",
                  description="Short name x is ambiguous", suggestion="Rename to index", confidence=0.7)

    results = [
        _agent_result("StyleAgent",       [f1]),
        _agent_result("PerformanceAgent", [f2]),
    ]

    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate(results, pr_url="https://github.com/x/y/pull/1")

    assert len(report.findings) == 1
    merged = report.findings[0]
    assert set(merged.source_agents) == {"StyleAgent", "PerformanceAgent"}
    # Longer description / suggestion wins
    assert merged.description == "Short name x is ambiguous"
    assert merged.suggestion == "Use longer name"


# ---------------------------------------------------------------------------
# Test 2 – SecurityAgent CRITICAL not downgraded
# ---------------------------------------------------------------------------

def test_security_critical_not_downgraded():
    """SecurityAgent CRITICAL must survive even when StyleAgent has low confidence."""
    sec_finding = _finding(
        file="auth.py", line_start=20, category="sql_injection",
        severity="CRITICAL", confidence=0.95,
        description="SQL injection risk", suggestion="Use parameterised query",
    )
    style_finding = _finding(
        file="auth.py", line_start=21, category="sql_injection",
        severity="LOW", confidence=0.3,
        description="Possible SQL concat", suggestion="Refactor",
    )

    results = [
        _agent_result("SecurityAgent", [sec_finding]),
        _agent_result("StyleAgent",    [style_finding]),
    ]

    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate(results)

    assert len(report.findings) == 1
    assert report.findings[0].severity == "CRITICAL"


# ---------------------------------------------------------------------------
# Test 3 – weighted confidence calculation
# ---------------------------------------------------------------------------

def test_weighted_confidence_calculation():
    """Weighted confidence = sum(w_i * c_i) / sum(w_i)."""
    # SecurityAgent weight=1.0, confidence=0.6
    # StyleAgent    weight=0.4, confidence=0.8
    # Expected: (1.0*0.6 + 0.4*0.8) / (1.0 + 0.4) = (0.6 + 0.32) / 1.4 = 0.92/1.4 ≈ 0.6571
    f_sec = _finding(
        file="b.py", line_start=5, category="high_complexity",
        severity="HIGH", confidence=0.6,
    )
    f_sty = _finding(
        file="b.py", line_start=6, category="high_complexity",
        severity="LOW", confidence=0.8,
    )

    results = [
        _agent_result("SecurityAgent", [f_sec]),
        _agent_result("StyleAgent",    [f_sty]),
    ]

    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate(results)

    assert len(report.findings) == 1
    merged = report.findings[0]

    expected_wc = (1.0 * 0.6 + 0.4 * 0.8) / (1.0 + 0.4)
    assert abs(merged.confidence - round(expected_wc, 4)) < 1e-4


# ---------------------------------------------------------------------------
# Test 4 – Markdown contains all four severity sections
# ---------------------------------------------------------------------------

def test_markdown_contains_all_severity_sections():
    """The rendered Markdown must include CRITICAL, HIGH, MEDIUM, and LOW sections."""
    findings = [
        _finding(severity="CRITICAL", line_start=1, category="sql_injection"),
        _finding(severity="HIGH",     line_start=2, category="logic"),
        _finding(severity="MEDIUM",   line_start=3, category="naming"),
        _finding(severity="LOW",      line_start=4, category="style"),
    ]
    results = [_agent_result("SecurityAgent", findings)]

    agg, _ = _aggregator_with_mock_summary("Four issues found.")
    report = agg.aggregate(results)

    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        assert sev in report.markdown_report, f"{sev} section missing from Markdown"
    assert "# Code Review Report" in report.markdown_report
    assert "## Executive Summary" in report.markdown_report
    assert "## Statistics" in report.markdown_report


# ---------------------------------------------------------------------------
# Test 5 – empty input
# ---------------------------------------------------------------------------

def test_empty_input_produces_clean_report():
    """No agent results → no findings, stats all zero, Markdown still valid."""
    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate([], pr_url="https://github.com/x/y/pull/99", task_id=7)

    assert report.findings == []
    assert report.stats["total"] == 0
    assert report.task_id == 7
    assert report.pr_url == "https://github.com/x/y/pull/99"
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        assert report.stats["by_severity"].get(sev, 0) == 0
    # Even with zero findings all severity sections should appear
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        assert sev in report.markdown_report


# ---------------------------------------------------------------------------
# Test 6 – stats dict structure
# ---------------------------------------------------------------------------

def test_stats_structure():
    """stats must contain 'total', 'by_severity', and 'by_agent' keys."""
    f1 = _finding(severity="HIGH", confidence=0.9)
    f2 = _finding(severity="LOW",  confidence=0.5, line_start=50)
    results = [
        _agent_result("LogicAgent",       [f1]),
        _agent_result("PerformanceAgent", [f2]),
    ]

    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate(results)

    assert "total" in report.stats
    assert "by_severity" in report.stats
    assert "by_agent" in report.stats
    assert report.stats["by_agent"]["LogicAgent"] == 1
    assert report.stats["by_agent"]["PerformanceAgent"] == 1


# ---------------------------------------------------------------------------
# Test 7 – findings from different files are NOT merged
# ---------------------------------------------------------------------------

def test_findings_different_files_not_merged():
    """Same category and line number but different files → two separate findings."""
    f1 = _finding(file="a.py", line_start=10, category="naming")
    f2 = _finding(file="b.py", line_start=10, category="naming")

    results = [
        _agent_result("StyleAgent", [f1, f2]),
    ]

    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate(results)

    assert len(report.findings) == 2
    files = {f.file for f in report.findings}
    assert files == {"a.py", "b.py"}


# ---------------------------------------------------------------------------
# Test 8 – executive summary from Claude is used
# ---------------------------------------------------------------------------

def test_executive_summary_from_claude():
    """The executive_summary field should contain the Claude-generated text."""
    expected = "This PR has one critical security issue."
    f1 = _finding(severity="CRITICAL", category="sql_injection")
    results = [_agent_result("SecurityAgent", [f1])]

    agg, _ = _aggregator_with_mock_summary(summary_text=expected)
    report = agg.aggregate(results)

    assert report.executive_summary == expected
    assert expected in report.markdown_report


@pytest.mark.parametrize("severity", ["LOW", "MEDIUM", "HIGH", "CRITICAL"])
@pytest.mark.parametrize("confidence", [0.0, 0.2, 0.99])
def test_single_finding_preserves_severity(severity, confidence):
    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate([
        _agent_result("LogicAgent", [_finding(severity=severity, confidence=confidence)]),
    ])
    assert report.findings[0].severity == severity


@pytest.mark.parametrize("reverse", [False, True])
def test_severity_uses_combined_weighted_votes(reverse):
    results = [
        _agent_result("SecurityAgent", [_finding(severity="HIGH", confidence=0.7)]),
        _agent_result("LogicAgent", [_finding(severity="LOW", confidence=0.8)]),
        _agent_result("StyleAgent", [_finding(severity="LOW", confidence=0.9)]),
    ]
    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate(list(reversed(results)) if reverse else results)
    assert report.findings[0].severity == "LOW"


@pytest.mark.parametrize("confidence", [0.0, 0.5])
def test_tied_severity_votes_choose_higher_risk(confidence):
    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate([
        _agent_result("LogicAgent", [_finding(severity="LOW", confidence=confidence)]),
        _agent_result("LogicAgent", [_finding(severity="HIGH", confidence=confidence)]),
    ])
    assert report.findings[0].severity == "HIGH"


def test_agent_stats_sum_findings_across_files():
    agg, _ = _aggregator_with_mock_summary()
    report = agg.aggregate([
        _agent_result("LogicAgent", [_finding(file="a.py"), _finding(file="a.py", line_start=50)]),
        _agent_result("LogicAgent", [_finding(file="b.py")]),
        _agent_result("LogicAgent", []),
        _agent_result("StyleAgent", []),
    ])
    assert report.stats["by_agent"] == {"LogicAgent": 3, "StyleAgent": 0}
