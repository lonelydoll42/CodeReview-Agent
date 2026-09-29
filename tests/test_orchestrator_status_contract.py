"""Regression tests for terminal review status and cache eligibility."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.orchestrator as module
from agents.aggregator import Aggregator, AggregatedReport
from agents.base import AgentResult
from agents.orchestrator import Orchestrator
from tools.github_client import FileDiff, PRDiff
from storage.models import TaskStatus


def _result(agent_name: str) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        findings=[],
        summary="clean",
        execution_time=0.0,
        token_used=0,
    )


def _orchestrator(monkeypatch, pr_diff: PRDiff, agents: list[object]) -> tuple[Orchestrator, AsyncMock, AsyncMock, AsyncMock]:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.agents = agents
    orchestrator.github = MagicMock()
    orchestrator.github.get_pr_diff.return_value = pr_diff
    orchestrator.aggregator = Aggregator(enable_llm_summary=False)
    orchestrator._mark_running = AsyncMock()
    persist = AsyncMock()
    orchestrator._persist = persist

    monkeypatch.setattr(module.settings, "MAX_PARALLEL_AGENTS", 4)
    monkeypatch.setattr(module.settings, "AGENT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(module.settings, "ENABLE_DEDUP_CACHE", True)
    monkeypatch.setattr(module.settings, "REVIEW_RULESET_VERSION", "test")
    monkeypatch.setattr(module.settings, "ENABLE_PR_COMMENT", False)
    monkeypatch.setattr(module.settings, "ENABLE_INLINE_COMMENT", False)
    monkeypatch.setattr(module, "set_agent_result", AsyncMock())
    set_cache = AsyncMock()
    monkeypatch.setattr(module, "set_dedup_task_id", set_cache)
    monkeypatch.setattr(module, "get_dedup_task_id", AsyncMock(return_value=None))
    complete = AsyncMock()
    failed = AsyncMock()
    monkeypatch.setattr(module, "notify_review_complete", complete)
    monkeypatch.setattr(module, "notify_review_failed", failed)
    return orchestrator, persist, set_cache, complete


class _GoodAgent:
    async def review(self, _file_diff):
        return _result(type(self).__name__)


class _FailingAgent:
    async def review(self, _file_diff):
        raise RuntimeError("provider unavailable")


@pytest.mark.asyncio
async def test_some_successful_attempts_are_partial_and_not_cached(monkeypatch) -> None:
    pr_diff = PRDiff(
        base_sha="base",
        head_sha="head",
        merge_base_sha="ancestor",
        files=[FileDiff(filename="app.py", language="python", patch="", added_lines=[(1, "x = 1")])],
    )
    orchestrator, persist, set_cache, complete = _orchestrator(
        monkeypatch,
        pr_diff,
        [_GoodAgent(), _FailingAgent()],
    )

    await orchestrator._run(1, "https://github.com/o/r/pull/1")

    report = persist.await_args.args[2]
    assert isinstance(report, AggregatedReport)
    assert report.review_status == TaskStatus.PARTIAL.value
    assert report.coverage[0].status == "partial"
    set_cache.assert_not_awaited()
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_agent_failures_are_failed_and_not_cached(monkeypatch) -> None:
    pr_diff = PRDiff(
        base_sha="base",
        head_sha="head",
        merge_base_sha="ancestor",
        files=[FileDiff(filename="app.py", language="python", patch="", added_lines=[(1, "x = 1")])],
    )
    orchestrator, persist, set_cache, complete = _orchestrator(
        monkeypatch,
        pr_diff,
        [_FailingAgent(), _FailingAgent()],
    )

    await orchestrator._run(2, "https://github.com/o/r/pull/2")

    report = persist.await_args.args[2]
    assert report.review_status == TaskStatus.FAILED.value
    assert report.coverage[0].status == "failed"
    assert set_cache.await_count == 0
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_skipped_files_are_uncovered_and_not_cached(monkeypatch) -> None:
    pr_diff = PRDiff(
        base_sha="base",
        head_sha="head",
        merge_base_sha="ancestor",
        files=[],
        skipped_files={"README.md": "unsupported language"},
    )
    orchestrator, persist, set_cache, complete = _orchestrator(
        monkeypatch,
        pr_diff,
        [_GoodAgent()],
    )

    await orchestrator._run(3, "https://github.com/o/r/pull/3")

    report = persist.await_args.args[2]
    assert report.review_status == TaskStatus.UNCOVERED.value
    assert report.coverage[0].status == "unsupported"
    assert report.coverage[0].reason == "unsupported language"
    set_cache.assert_not_awaited()
    complete.assert_not_awaited()
