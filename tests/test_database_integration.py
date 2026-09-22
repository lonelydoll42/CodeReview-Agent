"""Opt-in PostgreSQL tests using a disposable schema, without GitHub or LLM calls."""
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from agents.aggregator import AggregatedReport
from agents.base import AgentResult, Finding
from agents.orchestrator import Orchestrator
from api.main import _build_recent_review_items, _build_stats_summary, _build_top_categories, _build_trends
from storage.models import Base, ReviewTask, TaskStatus


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires disposable PostgreSQL TEST_DATABASE_URL")
async def test_cached_report_is_queryable_and_dashboard_sql_runs():
    schema = "review_test_" + uuid.uuid4().hex
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_async_engine(
        os.environ["TEST_DATABASE_URL"], connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(isolated, expire_on_commit=False)
    try:
        async with isolated.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            original = ReviewTask(pr_url="https://github.com/owner/repo/pull/1", status=TaskStatus.PENDING)
            duplicate = ReviewTask(pr_url=original.pr_url, status=TaskStatus.PENDING)
            session.add_all([original, duplicate])
            await session.commit()

        finding = Finding(file="app.py", line_start=3, line_end=3, severity="HIGH",
                          category="logic", description="bad", suggestion="fix", confidence=0.9)
        result = AgentResult(agent_name="LogicAgent", findings=[finding], summary="one", execution_time=0.1, token_used=10)
        report = AggregatedReport(task_id=original.id, pr_url=original.pr_url, findings=[],
                                  executive_summary="one", markdown_report="# Report", stats={},
                                  pr_metadata={"head_sha": "head"})
        orchestrator = Orchestrator.__new__(Orchestrator)
        with patch("agents.orchestrator.AsyncSessionLocal", sessions), patch("agents.orchestrator.set_task_status", new_callable=AsyncMock):
            await orchestrator._persist(original.id, [result], report)
            assert await orchestrator._reuse_report(original.id, duplicate.id, duplicate.pr_url)
            assert not await orchestrator._reuse_report(999999, duplicate.id, duplicate.pr_url)

        async with sessions() as session:
            task = (await session.execute(select(ReviewTask).where(ReviewTask.id == duplicate.id).options(
                selectinload(ReviewTask.report), selectinload(ReviewTask.results),
            ))).scalar_one()
            assert task.status == TaskStatus.COMPLETED
            copied_report = AggregatedReport.model_validate_json(task.report.final_report)
            assert copied_report.task_id == duplicate.id
            assert copied_report.pr_metadata["head_sha"] == "head"
            assert len(task.results) == 1
            summary = await _build_stats_summary(session)
            assert summary.total_tasks == 2 and summary.completed == 2
            assert summary.total_findings == 2
            assert summary.by_severity[0].severity == "HIGH"
            assert (await _build_top_categories(10, session))[0].category == "logic"
            assert sum(p.count for p in (await _build_trends(30, session)).findings) == 2
            recent = await _build_recent_review_items(10, session)
            assert len(recent) == 2 and all(row.has_report for row in recent)
    finally:
        await isolated.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
