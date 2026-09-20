"""Full-file analysis must preserve source coordinates and change scope."""
from unittest.mock import MagicMock, patch

import pytest

from agents.base import FileDiff
from agents.logic_agent import LogicAgent
from agents.performance_agent import PerformanceAgent
from agents.security_agent import SecurityAgent
from tools.ast_parser import ASTParser
from tools.semgrep_runner import SecurityIssue


@pytest.mark.asyncio
async def test_logic_agent_parses_enclosing_function():
    source = "def example(value):\n    if value:\n        return 2\n    return 0\n"
    fd = FileDiff(filename="app.py", added_lines=[(3, "        return 2")], full_source=source)
    agent = LogicAgent.__new__(LogicAgent)
    agent._parser = ASTParser()
    agent._call_claude = MagicMock(return_value=([], 0))
    with patch.object(agent._parser, "parse_python", wraps=agent._parser.parse_python) as parse:
        await agent.review(fd)
    parse.assert_called_once_with(source)
    assert "example(line 1" in agent._call_claude.call_args.args[2]


@pytest.mark.asyncio
async def test_performance_agent_uses_full_source():
    fd = FileDiff(filename="app.py", added_lines=[(2, "    return 1")], full_source="def f():\n    return 1\n")
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent._parser = MagicMock()
    agent._call_claude = MagicMock(return_value=([], 0))
    await agent.review(fd)
    agent._parser.get_complexity.assert_called_once_with(fd.full_source, "python")


def test_security_filters_unchanged_findings_and_preserves_coordinates():
    fd = FileDiff(filename="app.py", added_lines=[(3, '    password = "secret"')],
                  full_source='old_secret = "existing"\ndef f():\n    password = "secret"\n')
    agent = SecurityAgent.__new__(SecurityAgent)
    agent._semgrep = MagicMock()
    agent._semgrep.scan.return_value = [
        SecurityIssue(rule_id="secret", severity="WARNING", message="secret", line=line)
        for line in (1, 3)
    ]
    assert [issue.line for issue in agent._run_semgrep(fd)] == [3]
    agent._semgrep.scan.assert_called_once_with(fd.full_source, "python")


def test_legacy_partial_source_does_not_shift_line_numbers():
    fd = FileDiff(filename="app.py", added_lines=[(3, "a = 1"), (5, "b = 2")])
    assert fd.analysis_source().splitlines() == ["", "", "a = 1", "", "b = 2"]
