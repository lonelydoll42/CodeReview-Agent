"""Deletion-only changes must reach the risk-focused agents."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.base import FileDiff
from agents.change_context import deletion_anchors
from agents.errors import AgentToolOutputError
from agents.logic_agent import LogicAgent
from agents.security_agent import SecurityAgent


class _Client:
    def __init__(self, findings: list[dict] | None) -> None:
        self.prompts: list[str] = []
        self.findings = findings
        self.messages = self

    async def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        content = [] if self.findings is None else [SimpleNamespace(
            type="tool_use",
            name=kwargs["tools"][0]["name"],
            input={"findings": self.findings},
        )]
        return SimpleNamespace(
            content=content,
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )


def _finding(category: str, description: str, line: int) -> dict:
    return {
        "line_start": line,
        "line_end": line,
        "severity": "HIGH",
        "category": category,
        "description": description,
        "suggestion": "Restore the removed guard.",
        "confidence": 0.9,
        "cwe": "CWE-862",
    }


@pytest.mark.asyncio
async def test_deleted_authorization_is_reviewed_on_head_side() -> None:
    source = "def delete_user(user):\n    delete_record(user)\n"
    diff = FileDiff(
        filename="users.py", full_source=source,
        removed_lines=[(2, "    require_admin(user)")],
        raw_diff="@@ -1,3 +1,2 @@\n def delete_user(user):\n-    require_admin(user)\n     delete_record(user)",
    )
    client = _Client([_finding("missing_auth", "Authorization was removed.", 2)])
    agent = SecurityAgent(client=client)

    result = await agent.review(diff)

    assert result.findings[0].line_start == 2
    assert result.findings[0].category == "missing_auth"
    assert result.token_used == 5
    assert len(client.prompts) == 1
    assert "-    require_admin(user)" in client.prompts[0]
    assert "head    2 |     delete_record(user)" in client.prompts[0]


@pytest.mark.asyncio
async def test_mixed_diff_still_reviews_removed_guard() -> None:
    diff = FileDiff(
        filename="users.py", full_source="def delete_user(user):\n    delete_record(user)\n",
        added_lines=[(2, "    delete_record(user)")],
        removed_lines=[(2, "    require_admin(user)")],
        raw_diff=(
            "@@ -1,2 +1,2 @@\n def delete_user(user):\n"
            "-    require_admin(user)\n+    delete_record(user)"
        ),
    )
    client = _Client([])
    agent = SecurityAgent(client=client)
    agent._semgrep.scan = lambda *_: []

    await agent.review(diff)

    assert len(client.prompts) == 2
    assert "-    require_admin(user)" in client.prompts[1]


@pytest.mark.asyncio
async def test_deleted_error_handling_is_reviewed() -> None:
    source = "def save(item):\n    write_to_disk(item)\n"
    diff = FileDiff(
        filename="storage.py", full_source=source,
        removed_lines=[(2, "    try:"), (4, "    except OSError:"), (5, "        rollback()")],
        raw_diff=(
            "@@ -1,5 +1,2 @@\n def save(item):\n-    try:\n"
            "-        write_to_disk(item)\n-    except OSError:\n-        rollback()\n"
            "+    write_to_disk(item)"
        ),
    )
    client = _Client([_finding("missing_error_handling", "Rollback handling was removed.", 2)])
    agent = LogicAgent(client=client)

    result = await agent.review(diff)

    assert result.findings[0].category == "missing_error_handling"
    assert len(client.prompts) == 1
    assert "-        rollback()" in client.prompts[0]
    assert "head    2 |     write_to_disk(item)" in client.prompts[0]


@pytest.mark.asyncio
async def test_missing_tool_output_on_deletion_is_failure() -> None:
    diff = FileDiff(
        filename="users.py", full_source="def run():\n    pass\n",
        removed_lines=[(2, "    require_admin(user)")],
    )
    agent = SecurityAgent(client=_Client(None))

    with pytest.raises(AgentToolOutputError):
        await agent.review(diff)


def test_deletion_anchors_follow_multiple_hunks() -> None:
    diff = FileDiff(
        filename="example.py", full_source="a\nb\nc\nd\n",
        removed_lines=[(2, "guard_a()"), (5, "guard_b()")],
        raw_diff=(
            "@@ -1,3 +1,2 @@\n a\n-guard_a()\n b\n"
            "@@ -4,3 +3,2 @@\n c\n-guard_b()\n d"
        ),
    )

    assert deletion_anchors(diff) == [(2, "guard_a()", 2), (5, "guard_b()", 4)]
