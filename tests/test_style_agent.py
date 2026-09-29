"""Tests for StyleAgent.

The tests mock the Anthropic client so no real API key is needed.
Three scenarios are covered:
  1. Clean code  → no findings
  2. Naming problems  → at least one 'naming' finding
  3. Magic numbers + missing docstrings  → findings for both categories
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import FileDiff
from agents.style_agent import StyleAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_use_block(findings: list[dict[str, Any]]) -> MagicMock:
    """Return a mock content block that looks like a tool-use response."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "report_style_findings"
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
    """Clean, well-written code should produce zero findings."""
    added = [
        (1,  "def calculate_total(prices: list[float]) -> float:"),
        (2,  '    """Return the sum of all prices."""'),
        (3,  "    return sum(prices)"),
    ]
    mock_resp = _make_response([])   # model reports nothing

    agent = StyleAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    assert result.agent_name == "StyleAgent"
    assert result.findings == []
    assert result.token_used == 150
    assert "No style issues" in result.summary


# ---------------------------------------------------------------------------
# Test 2 – naming problems
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_naming_findings():
    """Code with bad variable names should trigger naming findings."""
    added = [
        (10, "def processData(d):"),
        (11, "    x = d * 2"),
        (12, "    tmp = x + 1"),
        (13, "    return tmp"),
    ]
    raw_findings = [
        {
            "line_start": 10, "line_end": 10,
            "severity": "MEDIUM",
            "category": "naming",
            "description": "Function name 'processData' uses camelCase instead of snake_case.",
            "suggestion": "Rename to 'process_data'.",
            "confidence": 0.95,
        },
        {
            "line_start": 10, "line_end": 10,
            "severity": "LOW",
            "category": "naming",
            "description": "Parameter 'd' is too short and uninformative.",
            "suggestion": "Use a descriptive name such as 'data'.",
            "confidence": 0.90,
        },
        {
            "line_start": 11, "line_end": 12,
            "severity": "LOW",
            "category": "naming",
            "description": "Variables 'x' and 'tmp' are cryptic single-letter / generic names.",
            "suggestion": "Use meaningful names that reflect their purpose.",
            "confidence": 0.88,
        },
    ]
    mock_resp = _make_response(raw_findings)

    agent = StyleAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    naming_findings = [f for f in result.findings if f.category == "naming"]
    assert len(naming_findings) == 3
    assert all(f.file == "example.py" for f in result.findings)
    assert result.token_used == 150
    assert "naming" in result.summary.lower() or len(result.findings) > 0


# ---------------------------------------------------------------------------
# Test 3 – magic numbers + missing docstrings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_magic_number_and_missing_docstring():
    """Code with magic numbers and missing docstrings triggers both categories."""
    added = [
        (20, "def apply_discount(price):"),
        (21, "    if price > 1000:"),
        (22, "        return price * 0.85"),
        (23, "    return price * 0.95"),
    ]
    raw_findings = [
        {
            "line_start": 20, "line_end": 20,
            "severity": "MEDIUM",
            "category": "missing_docstring",
            "description": "Public function 'apply_discount' lacks a docstring.",
            "suggestion": "Add a docstring explaining parameters, return value, and behaviour.",
            "confidence": 0.97,
        },
        {
            "line_start": 21, "line_end": 21,
            "severity": "MEDIUM",
            "category": "magic_number",
            "description": "Magic number 1000 used directly in comparison.",
            "suggestion": "Extract to a named constant, e.g. DISCOUNT_THRESHOLD = 1000.",
            "confidence": 0.93,
        },
        {
            "line_start": 22, "line_end": 23,
            "severity": "MEDIUM",
            "category": "magic_number",
            "description": "Magic numbers 0.85 and 0.95 used as discount multipliers.",
            "suggestion": "Define LARGE_ORDER_DISCOUNT = 0.85 and STANDARD_DISCOUNT = 0.95.",
            "confidence": 0.92,
        },
    ]
    mock_resp = _make_response(raw_findings)

    agent = StyleAgent(api_key="test-key")
    with patch.object(
        agent._client.messages,
        "create",
        new=AsyncMock(return_value=mock_resp),
    ):
        result = await agent.review(_diff(added))

    categories = {f.category for f in result.findings}
    assert "magic_number" in categories
    assert "missing_docstring" in categories
    assert len(result.findings) == 3
    assert result.execution_time >= 0
    assert "3 issue" in result.summary
