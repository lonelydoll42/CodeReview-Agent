"""Tests for API helpers and route handlers."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.main import (
    _dashboard_cache,
    _enqueue_review,
    create_review,
    get_review,
    list_recent_reviews,
    ReviewRequest,
)
from storage.models import TaskStatus


@pytest.mark.asyncio
async def test_enqueue_review_creates_task_and_launches_orchestrator():
    """The shared enqueue helper should persist the task and start Orchestrator."""
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def _refresh(task):
        task.id = 42

    db.refresh = AsyncMock(side_effect=_refresh)

    scheduled: dict[str, object] = {}

    def _capture_task(coro):
        scheduled["coro"] = coro
        coro.close()
        return MagicMock()

    with (
        patch("api.main.set_task_status", new_callable=AsyncMock) as mock_set_task_status,
        patch("api.main.Orchestrator") as mock_orchestrator_cls,
        patch("api.main.asyncio.create_task", side_effect=_capture_task) as mock_create_task,
    ):
        _dashboard_cache[30] = (999999.0, MagicMock())
        mock_orchestrator_cls.return_value.run = AsyncMock()
        task = await _enqueue_review("https://github.com/owner/repo/pull/1", db)

    assert task.id == 42
    assert task.status == TaskStatus.PENDING
    mock_set_task_status.assert_awaited_once_with(42, TaskStatus.PENDING.value)
    mock_orchestrator_cls.return_value.run.assert_called_once_with(
        task_id=42,
        pr_url="https://github.com/owner/repo/pull/1",
    )
    mock_create_task.assert_called_once()
    assert "coro" in scheduled
    assert _dashboard_cache == {}


@pytest.mark.asyncio
async def test_create_review_uses_enqueue_helper():
    """POST /review handler should delegate to the shared enqueue helper."""
    task = MagicMock()
    task.id = 7
    task.status = TaskStatus.PENDING

    with patch("api.main._enqueue_review", new_callable=AsyncMock, return_value=task) as mock_enqueue:
        response = await create_review(
            ReviewRequest(pr_url="https://github.com/owner/repo/pull/7"),
            db=AsyncMock(),
        )

    mock_enqueue.assert_awaited_once()
    assert response.task_id == 7
    assert response.status == "pending"
    assert "GET /review/{task_id}" in response.message


@pytest.mark.asyncio
async def test_list_recent_reviews_returns_lightweight_task_rows():
    """Recent reviews endpoint should flatten task rows for the UI."""
    now = datetime.now(timezone.utc)
    row = MagicMock()
    row.id = 9
    row.pr_url = "https://github.com/owner/repo/pull/9"
    row.status = TaskStatus.COMPLETED
    row.created_at = now
    row.updated_at = now
    row.findings_count = 2
    row.has_report = True

    execute_result = MagicMock()
    execute_result.all.return_value = [row]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    items = await list_recent_reviews(limit=10, db=db)

    assert len(items) == 1
    assert items[0].task_id == 9
    assert items[0].status == "completed"
    assert items[0].findings_count == 2
    assert items[0].has_report is True


@pytest.mark.asyncio
async def test_get_review_can_skip_agent_results():
    """Task detail can omit heavy per-agent results for lighter UI polling."""
    now = datetime.now(timezone.utc)
    report = MagicMock()
    report.final_report = "{}"
    report.markdown_report = "# report"
    report.created_at = now

    task = MagicMock()
    task.id = 11
    task.pr_url = "https://github.com/owner/repo/pull/11"
    task.status = TaskStatus.COMPLETED
    task.created_at = now
    task.updated_at = now
    task.results = [MagicMock(agent_name="SecurityAgent", findings={"findings": [1]}, confidence=0.9)]
    task.report = report

    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = task

    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    with patch("api.main.get_task_status", new_callable=AsyncMock, return_value=None):
        response = await get_review(task_id=11, include_results=False, db=db)

    assert response.task_id == 11
    assert response.status == "completed"
    assert response.results == []
    assert response.report is not None
