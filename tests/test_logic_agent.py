"""Tests for LogicAgent.

Mocks the Anthropic client so no real API key is needed.
Three scenarios are covered:
  1. Clean code              → no findings
  2. Bare except             → at least one 'bare_except' finding
  3. Null dereference        → at least one 'null_dereference' finding
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import FileDiff
from agents.logic_agent import LogicAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_use_block(findings: list[dict[str, Any]]) -> MagicMock:
    """Return a mock content block that looks like a tool-use response."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "report_logic_findings"
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
    """Well-written code should produce zero findings."""
    added = [
        (1, "def calculate_total(prices: list[float]) -> float:"),
        (2, '    """Return the sum of all prices."""'),
        (3, "    if not prices:"),
        (4, "        return 0.0"),
        (5, "    return sum(prices)"),
    ]
    mock_resp = _make_response([])

    agent = LogicAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    assert result.findings == []
    assert result.agent_name == "LogicAgent"
    assert result.execution_time >= 0
    assert "No logic issues" in result.summary


# ---------------------------------------------------------------------------
# Test 2 – bare except / swallowed exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bare_except_detected():
    """Code with a bare except should produce a bare_except finding."""
    added = [
        (10, "def read_config(path):"),
        (11, "    try:"),
        (12, "        with open(path) as f:"),
        (13, "            return f.read()"),
        (14, "    except:"),
        (15, "        pass"),
    ]
    raw_findings = [
        {
            "line_start": 14, "line_end": 15,
            "severity": "HIGH",
            "category": "bare_except",
            "description": "Bare `except:` swallows all exceptions including KeyboardInterrupt.",
            "suggestion": "Catch specific exceptions, e.g. `except (OSError, IOError) as e:` and log or re-raise.",
            "confidence": 0.95,
        },
    ]
    mock_resp = _make_response(raw_findings)

    agent = LogicAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    categories = {f.category for f in result.findings}
    assert "bare_except" in categories
    assert result.findings[0].severity == "HIGH"
    assert result.findings[0].file == "example.py"
    assert result.token_used == 150
    assert "1 issue" in result.summary


# ---------------------------------------------------------------------------
# Test 3 – null dereference without guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_null_dereference_detected():
    """Code that dereferences a potentially-None value should be flagged."""
    added = [
        (20, "def get_username(user):"),
        (21, "    return user.profile.name"),
    ]
    raw_findings = [
        {
            "line_start": 21, "line_end": 21,
            "severity": "HIGH",
            "category": "null_dereference",
            "description": "`user` or `user.profile` may be None, causing AttributeError.",
            "suggestion": "Add a None guard: `if user and user.profile: return user.profile.name`",
            "confidence": 0.85,
        },
    ]
    mock_resp = _make_response(raw_findings)

    agent = LogicAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    categories = {f.category for f in result.findings}
    assert "null_dereference" in categories
    assert result.findings[0].confidence >= 0.7
    assert "1 issue" in result.summary
