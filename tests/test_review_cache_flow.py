"""Do not advertise incomplete or unpersisted work as reusable reviews."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.orchestrator as module
from agents.base import AgentResult
from agents.orchestrator import Orchestrator
from tools.github_client import FileDiff, PRDiff


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_error, agent_error", [(False, False), (True, False), (False, True)])
async def test_cache_written_only_after_successful_persistence(monkeypatch, persist_error, agent_error):
    events = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.github = MagicMock()
    orchestrator.github.get_pr_diff.return_value = PRDiff(
        base_sha="base", head_sha="head", merge_base_sha="ancestor",
        files=[FileDiff(filename="app.py", language="python", patch="", added_lines=[(1, "x = 1")])],
    )
    result = AgentResult(agent_name="LogicAgent", findings=[], summary="", execution_time=0, token_used=0)
    agent = MagicMock()
    agent.review = AsyncMock(return_value=result, side_effect=RuntimeError("agent failed") if agent_error else None)
    orchestrator.agents = [agent]
    orchestrator.aggregator = MagicMock()

    async def persist(*args):
        events.append("persist")
        if persist_error:
            raise RuntimeError("database failed")

    async def cache(*args):
        events.append("cache")

    orchestrator._persist = persist
    monkeypatch.setattr(module, "settings", SimpleNamespace(
        ENABLE_DEDUP_CACHE=True, REVIEW_RULESET_VERSION="1", AGENT_TIMEOUT_SECONDS=1,
        ENABLE_PR_COMMENT=False, ENABLE_INLINE_COMMENT=False,
    ))
    for name in ("set_task_status", "set_agent_result", "notify_review_complete"):
        monkeypatch.setattr(module, name, AsyncMock())
    monkeypatch.setattr(module, "get_dedup_task_id", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "set_dedup_task_id", cache)
    if persist_error:
        with pytest.raises(RuntimeError, match="database failed"):
            await orchestrator.run(1, "url")
    else:
        await orchestrator.run(1, "url")
    assert events == (["persist"] if persist_error or agent_error else ["persist", "cache"])
