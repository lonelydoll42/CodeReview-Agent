"""Regression tests for the shared async model runtime contract."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agents.errors import (
    AgentProviderError,
    AgentToolOutputError,
    AgentRuntimeConfig,
    AgentRuntime,
    parse_tool_findings,
)


class _Messages:
    def __init__(self, handler):
        self._handler = handler

    def create(self, **kwargs):
        return self._handler(**kwargs)


class _Client:
    def __init__(self, handler):
        self.messages = _Messages(handler)
        self.closed = False

    async def close(self):
        self.closed = True


def _tool_response(tool_name: str, findings: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name=tool_name,
                input={"findings": findings},
            )
        ]
    )


def test_empty_findings_is_a_successful_clean_result() -> None:
    findings = parse_tool_findings(
        _tool_response("report_findings", []),
        expected_tool_name="report_findings",
        filename="app.py",
        max_line=10,
        agent_name="TestAgent",
    )

    assert findings == []


def test_runtime_config_reads_project_settings_when_env_is_absent(monkeypatch) -> None:
    """Values in the repository .env flow through the shared settings model."""
    monkeypatch.delenv("ANTHROPIC_REQUEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("ANTHROPIC_MAX_RETRIES", raising=False)
    monkeypatch.setattr(
        "agents.errors.settings",
        SimpleNamespace(
            ANTHROPIC_REQUEST_TIMEOUT_SECONDS=12.5,
            ANTHROPIC_MAX_RETRIES=4,
        ),
    )

    config = AgentRuntimeConfig.from_environment()

    assert config.request_timeout_seconds == 12.5
    assert config.max_retries == 4


def test_missing_tool_output_is_a_failure() -> None:
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="done")])

    with pytest.raises(AgentToolOutputError) as exc_info:
        parse_tool_findings(
            response,
            expected_tool_name="report_findings",
            filename="app.py",
            max_line=10,
            agent_name="TestAgent",
        )

    assert exc_info.value.reason_code == "missing_tool_output"


def test_malformed_finding_is_a_failure_instead_of_being_dropped() -> None:
    response = _tool_response(
        "report_findings",
        [{"line_start": 1, "line_end": 1}],
    )

    with pytest.raises(AgentToolOutputError) as exc_info:
        parse_tool_findings(
            response,
            expected_tool_name="report_findings",
            filename="app.py",
            max_line=10,
            agent_name="TestAgent",
        )

    assert exc_info.value.reason_code == "malformed_tool_output"


@pytest.mark.asyncio
async def test_provider_timeout_is_typed_and_bounded() -> None:
    async def slow_request(**_kwargs):
        await asyncio.sleep(60)

    runtime = AgentRuntime(
        agent_name="TestAgent",
        client=_Client(slow_request),
        request_timeout=0.01,
    )

    with pytest.raises(AgentProviderError) as exc_info:
        await runtime.create_message(model="test")

    assert exc_info.value.reason_code == "provider_request_timeout"


@pytest.mark.asyncio
async def test_provider_cancellation_propagates_and_client_can_close() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def cancellable_request(**_kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client = _Client(cancellable_request)
    runtime = AgentRuntime(agent_name="TestAgent", client=client, request_timeout=60)
    pending = asyncio.create_task(runtime.create_message(model="test"))
    await started.wait()
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending

    await runtime.close()
    assert cancelled.is_set()
    assert client.closed
