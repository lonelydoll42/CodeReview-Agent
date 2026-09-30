"""Regression tests for the bundled Semgrep rules and failure contract."""
from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import patch

import pytest

from agents.base import FileDiff
from agents.errors import AgentRuntimeError
from agents.security_agent import SecurityAgent
from tools.semgrep_runner import SemgrepRunner, SemgrepScanError


def _real_semgrep_runner() -> SemgrepRunner:
    if shutil.which("semgrep") is None:
        pytest.skip("semgrep executable is not installed")
    return SemgrepRunner()


def test_python_sql_patterns_match_each_alternative() -> None:
    runner = _real_semgrep_runner()
    code = "\n".join(
        [
            'db.execute("SELECT * FROM users WHERE id = %s" % user_id)',
            'db.execute("SELECT * FROM users WHERE id = " + user_id)',
            'db.execute(f"SELECT * FROM users WHERE id = {user_id}")',
        ]
    )

    issues = runner.scan(code, "python")

    assert {
        issue.line
        for issue in issues
        if issue.rule_id.endswith(".sql-injection-python-format")
    } == {1, 2, 3}


def test_javascript_sql_patterns_match_query_or_execute() -> None:
    runner = _real_semgrep_runner()
    code = "\n".join(
        [
            'db.query("SELECT * FROM users WHERE id = " + userId);',
            'db.execute("SELECT * FROM users WHERE id = " + userId);',
        ]
    )

    issues = runner.scan(code, "javascript")

    assert {
        issue.line
        for issue in issues
        if issue.rule_id.endswith(".sql-injection-js")
    } == {1, 2}


def test_parameterized_python_query_is_not_reported_as_sql_injection() -> None:
    runner = _real_semgrep_runner()

    issues = runner.scan(
        'db.execute("SELECT * FROM users WHERE id = %s", (user_id,))',
        "python",
    )

    assert not any("sql-injection" in issue.rule_id for issue in issues)


@pytest.mark.parametrize(
    ("returncode", "payload", "reason_code"),
    [
        (2, {"results": []}, "semgrep_execution_failed"),
        (0, {"results": [], "errors": [{"message": "invalid rule"}]}, "semgrep_reported_error"),
        (0, {"results": "not-a-list"}, "semgrep_invalid_output"),
    ],
)
def test_semgrep_failures_are_not_returned_as_clean_results(
    returncode: int,
    payload: dict[str, object],
    reason_code: str,
) -> None:
    runner = SemgrepRunner()
    runner._semgrep_available = True
    completed = subprocess.CompletedProcess(
        args=["semgrep"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="semgrep failed",
    )

    with patch("tools.semgrep_runner.subprocess.run", return_value=completed):
        with pytest.raises(SemgrepScanError) as exc_info:
            runner.scan("value = 1", "python")

    assert exc_info.value.reason_code == reason_code


def test_semgrep_invalid_json_is_a_scan_failure() -> None:
    runner = SemgrepRunner()
    runner._semgrep_available = True
    completed = subprocess.CompletedProcess(
        args=["semgrep"],
        returncode=0,
        stdout="not-json",
        stderr="",
    )

    with patch("tools.semgrep_runner.subprocess.run", return_value=completed):
        with pytest.raises(SemgrepScanError) as exc_info:
            runner.scan("value = 1", "python")

    assert exc_info.value.reason_code == "semgrep_invalid_output"


def test_semgrep_timeout_is_a_scan_failure() -> None:
    runner = SemgrepRunner()
    runner._semgrep_available = True

    with patch(
        "tools.semgrep_runner.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["semgrep"], timeout=60),
    ):
        with pytest.raises(SemgrepScanError) as exc_info:
            runner.scan("value = 1", "python")

    assert exc_info.value.reason_code == "semgrep_timeout"


def test_security_agent_preserves_semgrep_failure_code() -> None:
    agent = SecurityAgent.__new__(SecurityAgent)
    agent._semgrep = SemgrepRunner()
    diff = FileDiff(filename="app.py", added_lines=[(1, "value = 1")])

    with patch.object(
        agent._semgrep, "scan", side_effect=SemgrepScanError("semgrep_reported_error")
    ):
        with pytest.raises(AgentRuntimeError) as exc_info:
            agent._run_semgrep(diff)

    assert exc_info.value.reason_code == "semgrep_reported_error"
