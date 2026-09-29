"""Contract tests for asynchronous Agent provider/runtime behavior."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from agents.errors import AgentProviderError, AgentToolOutputError
from agents.base import FileDiff
from agents.security_agent import SecurityAgent
from agents.style_agent import StyleAgent


def _response(findings: list[dict[str, Any]], *, tool_name: str = "report_style_findings") -> Any:
    block = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        input={"findings": findings},
    )
    usage = SimpleNamespace(input_tokens=2, output_tokens=3)
    return SimpleNamespace(content=[block], usage=usage)


def _diff() -> FileDiff:
    return FileDiff(
        filename="example.py",
        language="python",
        added_lines=[(1, "def f():"), (2, "    return 1")],
    )


class _Messages:
    def __init__(self, create: Any) -> None:
        self.create = create


class _Client:
    def __init__(self, create: Any) -> None:
        self.messages = _Messages(create)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_valid_empty_tool_report_is_a_success() -> None:
    async def create(**_: Any) -> Any:
        return _response([])

    agent = StyleAgent(client=_Client(create))
    result = await agent.review(_diff())

    assert result.findings == []
    assert result.token_used == 5


@pytest.mark.asyncio
async def test_missing_tool_report_is_an_explicit_failure() -> None:
    async def create(**_: Any) -> Any:
        return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=0, output_tokens=0))

    agent = StyleAgent(client=_Client(create))
    with pytest.raises(AgentToolOutputError) as exc_info:
        await agent.review(_diff())

    assert exc_info.value.reason_code == "missing_tool_output"


@pytest.mark.asyncio
async def test_malformed_coordinates_are_not_silently_dropped() -> None:
    async def create(**_: Any) -> Any:
        return _response([
            {
                "line_start": 0,
                "line_end": 1,
                "severity": "LOW",
                "category": "naming",
                "description": "bad coordinate",
                "suggestion": "fix it",
                "confidence": 0.8,
            }
        ])

    agent = StyleAgent(client=_Client(create))
    with pytest.raises(AgentToolOutputError) as exc_info:
        await agent.review(_diff())

    assert exc_info.value.reason_code == "invalid_finding_coordinates"


@pytest.mark.asyncio
async def test_provider_timeout_is_bounded_and_typed() -> None:
    started = asyncio.Event()

    async def create(**_: Any) -> Any:
        started.set()
        await asyncio.Event().wait()
        return _response([])  # pragma: no cover

    agent = StyleAgent(
        client=_Client(create),
        request_timeout=0.01,
        max_retries=0,
    )
    task = asyncio.create_task(agent.review(_diff()))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    with pytest.raises(AgentProviderError) as exc_info:
        await task

    assert exc_info.value.reason_code == "provider_request_timeout"


@pytest.mark.asyncio
async def test_provider_cancellation_propagates() -> None:
    started = asyncio.Event()

    async def create(**_: Any) -> Any:
        started.set()
        await asyncio.Event().wait()
        return _response([])  # pragma: no cover

    agent = StyleAgent(client=_Client(create), request_timeout=1.0)
    task = asyncio.create_task(agent.review(_diff()))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_semgrep_isolated_from_event_loop() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    tick = asyncio.Event()

    async def create(**_: Any) -> Any:
        return _response([], tool_name="report_security_findings")

    def blocking_scan(*_: Any) -> list[Any]:
        loop.call_soon_threadsafe(started.set)
        time.sleep(0.15)
        return []

    agent = SecurityAgent(client=_Client(create))
    agent._semgrep.scan = blocking_scan
    task = asyncio.create_task(agent.review(_diff()))
    loop.call_later(0.01, tick.set)
    wall_start = loop.time()
    try:
        await asyncio.wait_for(started.wait(), timeout=0.2)
        await asyncio.wait_for(tick.wait(), timeout=0.08)
        assert loop.time() - wall_start < 0.1
        await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
