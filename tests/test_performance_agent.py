"""Tests for PerformanceAgent.

The tests mock the Anthropic client so no real API key is needed.
Three scenarios are covered:
  1. Clean code  → no findings
  2. N+1 query pattern  → at least one 'n_plus_one' finding
  3. Clean async code with blocking I/O  → 'blocking_call' finding
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import FileDiff
from agents.performance_agent import PerformanceAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_use_block(findings: list[dict[str, Any]]) -> MagicMock:
    """Return a mock content block that looks like a tool-use response."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "report_performance_findings"
    block.input = {"findings": findings}
    return block


def _make_response(findings: list[dict[str, Any]]) -> MagicMock:
    """Return a mock anthropic Message containing one tool-use block."""
    resp = MagicMock()
    resp.content = [_make_tool_use_block(findings)]
    resp.usage = MagicMock(input_tokens=100, output_tokens=50)
    return resp


def _diff(added_lines: list[tuple[int, str]], filename: str = "example.py") -> FileDiff:
    return FileDiff(filename=filename, language="python", added_lines=added_lines)


# ---------------------------------------------------------------------------
# Test 1 – clean code, no findings expected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_findings_for_clean_code():
    """Efficient, well-written code should produce zero findings."""
    added = [
        (1,  "def calculate_total(prices: list[float]) -> float:"),
        (2,  '    """Return the sum of all prices."""'),
        (3,  "    return sum(prices)"),
    ]
    mock_resp = _make_response([])  # model reports nothing

    agent = PerformanceAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    assert result.findings == []
    assert result.agent_name == "PerformanceAgent"
    assert result.execution_time >= 0
    assert "No performance issues" in result.summary


# ---------------------------------------------------------------------------
# Test 2 – N+1 query pattern detected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_n_plus_one_detected():
    """ORM call inside a loop should be detected as n_plus_one."""
    added = [
        (10, "def get_user_orders(user_ids):"),
        (11, "    orders = []"),
        (12, "    for uid in user_ids:"),
        (13, "        user = User.objects.get(id=uid)  # DB query in loop"),
        (14, "        orders.append(user.orders.all())"),
        (15, "    return orders"),
    ]
    raw_findings = [
        {
            "line_start": 13, "line_end": 14,
            "severity": "HIGH",
            "category": "n_plus_one",
            "description": (
                "Database query 'User.objects.get' is called inside a loop, "
                "causing N+1 queries."
            ),
            "suggestion": (
                "Use 'User.objects.filter(id__in=user_ids).prefetch_related('orders')' "
                "to fetch all users in a single query."
            ),
            "confidence": 0.95,
        }
    ]
    mock_resp = _make_response(raw_findings)

    agent = PerformanceAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.category == "n_plus_one"
    assert finding.severity == "HIGH"
    assert finding.file == "example.py"
    assert finding.confidence == pytest.approx(0.95)
    assert "1 issue" in result.summary


# ---------------------------------------------------------------------------
# Test 3 – blocking I/O inside async function
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_blocking_call_in_async_detected():
    """Synchronous blocking call inside an async function should be flagged."""
    added = [
        (20, "async def fetch_data(url: str) -> bytes:"),
        (21, '    """Fetch raw data from the given URL."""'),
        (22, "    response = requests.get(url)  # blocking call in async context"),
        (23, "    return response.content"),
    ]
    raw_findings = [
        {
            "line_start": 22, "line_end": 22,
            "severity": "HIGH",
            "category": "blocking_call",
            "description": (
                "'requests.get' is a synchronous blocking call used inside an "
                "async function, which blocks the event loop."
            ),
            "suggestion": (
                "Replace with 'await httpx.AsyncClient().get(url)' or "
                "'await aiohttp.ClientSession().get(url)'."
            ),
            "confidence": 0.92,
        }
    ]
    mock_resp = _make_response(raw_findings)

    agent = PerformanceAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    categories = {f.category for f in result.findings}
    assert "blocking_call" in categories
    assert result.findings[0].severity == "HIGH"
    assert "1 issue" in result.summary
    assert result.token_used == 150  # 100 input + 50 output (mocked)
