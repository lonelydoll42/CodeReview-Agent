"""Tests for Orchestrator.

All external dependencies (DB, Redis, GitHub, Agents) are mocked so the
tests run without any live infrastructure.

Scenarios:
  1. Successful run: 4 agents return results, DB + Redis updated correctly.
  2. Agent timeout: timed-out agent is skipped, others still processed.
  3. GitHub diff fetch failure: task is marked FAILED, run exits early.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentResult, FileDiff, Finding
from agents.orchestrator import Orchestrator, _run_one_agent


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_finding(**kwargs: Any) -> Finding:
    defaults = dict(
        file="app.py", line_start=1, line_end=1,
        severity="MEDIUM", category="test",
        description="desc", suggestion="fix", confidence=0.7,
    )
    defaults.update(kwargs)
    return Finding(**defaults)


def _make_agent_result(agent_name: str, n_findings: int = 1) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        findings=[_make_finding(file="app.py") for _ in range(n_findings)],
        summary=f"{agent_name} found {n_findings} issue(s).",
        execution_time=0.5,
        token_used=100,
    )


def _file_diff() -> FileDiff:
    return FileDiff(
        filename="app.py",
        language="python",
        added_lines=[(1, "x = 1")],
    )


def _mock_pr_diff(language: str = "python") -> MagicMock:
    fd = MagicMock()
    fd.filename = "app.py"
    fd.language = language
    fd.added_lines = [(1, "x = 1")]
    fd.removed_lines = []
    fd.patch = ""
    fd.full_source = "x = 1\n"
    fd.status = "modified"
    fd.previous_filename = None
    pr = MagicMock()
    pr.files = [fd]
    pr.base_sha = "base"
    pr.head_sha = "head"
    pr.merge_base_sha = "ancestor"
    pr.metadata = {"base_sha": "base", "head_sha": "head"}
    return pr


# ---------------------------------------------------------------------------
# Test 1 – successful run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_success():
    """All 4 agents return results; DB and Redis are updated to COMPLETED."""
    agent_names = ["StyleAgent", "SecurityAgent", "LogicAgent", "PerformanceAgent"]
    results = {name: _make_agent_result(name) for name in agent_names}

    orchestrator = Orchestrator.__new__(Orchestrator)

    mock_agents = []
    for name in agent_names:
        a = MagicMock()
        a.__class__.__name__ = name
        a.review = AsyncMock(return_value=results[name])
        mock_agents.append(a)

    orchestrator.agents = mock_agents
    orchestrator.aggregator = MagicMock()
    orchestrator.github = MagicMock()
    orchestrator.github.get_head_commit_sha.return_value = None

    mock_report = MagicMock()
    mock_report.markdown_report = "# Report"
    mock_report.model_dump_json.return_value = "{}"
    orchestrator.aggregator.aggregate.return_value = mock_report
    orchestrator.github.get_pr_diff.return_value = _mock_pr_diff()

    with (
        patch("agents.orchestrator.set_task_status", new_callable=AsyncMock) as mock_set_status,
        patch("agents.orchestrator.set_agent_result", new_callable=AsyncMock),
        patch("agents.orchestrator.get_dedup_task_id", new_callable=AsyncMock, return_value=None),
        patch("agents.orchestrator.set_dedup_task_id", new_callable=AsyncMock),
        patch("agents.orchestrator.AsyncSessionLocal") as mock_session_cls,
    ):
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=MagicMock())
        mock_session.commit = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        await orchestrator.run(task_id=1, pr_url="https://github.com/owner/repo/pull/1")

    # Redis should have been called with running then completed
    statuses = [call.args[1] for call in mock_set_status.call_args_list]
    assert "running" in statuses
    assert "completed" in statuses

    # Aggregator must have been called with all 4 results
    orchestrator.aggregator.aggregate.assert_called_once()
    call_args = orchestrator.aggregator.aggregate.call_args
    passed_results = call_args.args[0] if call_args.args else call_args.kwargs["agent_results"]
    assert len(passed_results) == len(agent_names)


# ---------------------------------------------------------------------------
# Test 2 – one agent times out
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_agent_timeout():
    """A timed-out agent is skipped; remaining agents are still processed."""
    orchestrator = Orchestrator.__new__(Orchestrator)

    good_result = _make_agent_result("StyleAgent")

    good_agent = MagicMock()
    good_agent.__class__.__name__ = "StyleAgent"
    good_agent.review = AsyncMock(return_value=good_result)

    slow_agent = MagicMock()
    slow_agent.__class__.__name__ = "SecurityAgent"

    async def _slow(*_a: Any, **_kw: Any) -> AgentResult:  # never resolves quickly
        await asyncio.sleep(60)
        return _make_agent_result("SecurityAgent")  # pragma: no cover

    slow_agent.review = _slow

    orchestrator.agents = [good_agent, slow_agent]
    orchestrator.aggregator = MagicMock()
    orchestrator.github = MagicMock()
    orchestrator.github.get_head_commit_sha.return_value = None
    orchestrator.github.get_pr_diff.return_value = _mock_pr_diff()

    mock_report = MagicMock()
    mock_report.markdown_report = "# Report"
    mock_report.model_dump_json.return_value = "{}"
    orchestrator.aggregator.aggregate.return_value = mock_report

    with (
        patch("agents.orchestrator.set_task_status", new_callable=AsyncMock),
        patch("agents.orchestrator.set_agent_result", new_callable=AsyncMock),
        patch("agents.orchestrator.get_dedup_task_id", new_callable=AsyncMock, return_value=None),
        patch("agents.orchestrator.set_dedup_task_id", new_callable=AsyncMock),
        patch("agents.orchestrator.AsyncSessionLocal") as mock_session_cls,
        patch("agents.orchestrator.settings") as mock_settings,
    ):
        mock_settings.AGENT_TIMEOUT_SECONDS = 1  # force fast timeout
        mock_settings.REVIEW_RULESET_VERSION = "1"
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=MagicMock())
        mock_session.commit = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        await orchestrator.run(task_id=2, pr_url="https://github.com/owner/repo/pull/2")

    # Aggregator still called; only the good result passes through
    orchestrator.aggregator.aggregate.assert_called_once()
    call_args = orchestrator.aggregator.aggregate.call_args
    passed_results = call_args.args[0] if call_args.args else call_args.kwargs["agent_results"]
    assert any(r.agent_name == "StyleAgent" for r in passed_results)


# ---------------------------------------------------------------------------
# Test 3 – GitHub fetch failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_github_failure():
    """If fetching the PR diff raises, the task is marked FAILED and run exits."""
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.agents = []
    orchestrator.aggregator = MagicMock()
    orchestrator.github = MagicMock()
    orchestrator.github.get_head_commit_sha.return_value = None
    orchestrator.github.get_pr_diff.side_effect = RuntimeError("GitHub API down")

    with (
        patch("agents.orchestrator.set_task_status", new_callable=AsyncMock) as mock_set_status,
        patch("agents.orchestrator.set_agent_result", new_callable=AsyncMock),
        patch("agents.orchestrator.get_dedup_task_id", new_callable=AsyncMock, return_value=None),
        patch("agents.orchestrator.set_dedup_task_id", new_callable=AsyncMock),
        patch("agents.orchestrator.AsyncSessionLocal") as mock_session_cls,
    ):
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=MagicMock())
        mock_session.commit = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        await orchestrator.run(task_id=3, pr_url="https://github.com/owner/repo/pull/3")

    statuses = [call.args[1] for call in mock_set_status.call_args_list]
    assert "failed" in statuses
    # Aggregator should NOT have been called
    orchestrator.aggregator.aggregate.assert_not_called()
