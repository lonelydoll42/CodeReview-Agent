"""Regression tests for the shared event-loop Agent execution limiter."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import agents.orchestrator as module
from agents.base import AgentResult, FileDiff
from agents.orchestrator import _run_one_agent


def _result(name: str) -> AgentResult:
    return AgentResult(
        agent_name=name,
        findings=[],
        summary="clean",
        execution_time=0.0,
        token_used=0,
    )


def _file(name: str) -> FileDiff:
    return FileDiff(filename=name, language="python", added_lines=[(1, "value = 1")])


@pytest.mark.asyncio
async def test_concurrent_reviews_share_one_agent_slot(monkeypatch) -> None:
    started_first = asyncio.Event()
    started_second = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0

    class FirstAgent:
        async def review(self, _file_diff):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            started_first.set()
            try:
                await release_first.wait()
                return _result("FirstAgent")
            finally:
                active -= 1

    class SecondAgent:
        async def review(self, _file_diff):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            started_second.set()
            try:
                return _result("SecondAgent")
            finally:
                active -= 1

    monkeypatch.setattr(module.settings, "MAX_PARALLEL_AGENTS", 1)
    monkeypatch.setattr(module, "set_agent_result", AsyncMock())

    first = asyncio.create_task(_run_one_agent(FirstAgent(), _file("first.py"), 1, timeout=0.2))
    await started_first.wait()
    second = asyncio.create_task(_run_one_agent(SecondAgent(), _file("second.py"), 2, timeout=0.02))
    await asyncio.sleep(0.03)
    assert not started_second.is_set()

    release_first.set()
    results = await asyncio.gather(first, second)

    assert [result.agent_name for result in results if result is not None] == [
        "FirstAgent",
        "SecondAgent",
    ]
    assert max_active == 1


@pytest.mark.asyncio
async def test_timeout_releases_shared_agent_slot(monkeypatch) -> None:
    started_first = asyncio.Event()
    started_second = asyncio.Event()

    class HangingAgent:
        async def review(self, _file_diff):
            started_first.set()
            await asyncio.Event().wait()

    class FastAgent:
        async def review(self, _file_diff):
            started_second.set()
            return _result("FastAgent")

    monkeypatch.setattr(module.settings, "MAX_PARALLEL_AGENTS", 1)
    monkeypatch.setattr(module, "set_agent_result", AsyncMock())

    first = asyncio.create_task(_run_one_agent(HangingAgent(), _file("slow.py"), 1, timeout=0.01))
    await started_first.wait()
    second = asyncio.create_task(_run_one_agent(FastAgent(), _file("fast.py"), 2, timeout=0.1))
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result is None
    assert isinstance(second_result, AgentResult)
    assert started_second.is_set()


@pytest.mark.asyncio
async def test_invalid_agent_result_is_recorded_as_a_failed_attempt(monkeypatch) -> None:
    """A broken agent contract must not disappear from file coverage."""
    monkeypatch.setattr(module, "set_agent_result", AsyncMock())
    records = []

    class InvalidAgent:
        async def review(self, _file_diff):
            return {"findings": []}

    result = await _run_one_agent(
        InvalidAgent(),
        _file("invalid.py"),
        3,
        timeout=0.1,
        execution_records=records,
    )

    assert result is None
    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].error_code == "malformed_agent_result"
