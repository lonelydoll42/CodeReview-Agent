"""FastAPI application for CodeReview-Agent."""

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agents.orchestrator import Orchestrator
from config import settings
from storage.models import (
    Base,
    ReviewReport,
    ReviewResult,
    ReviewTask,
    TaskStatus,
    engine,
    get_db,
)
from storage.cache import get_task_status, set_task_status


FINDINGS_COUNT_EXPR = func.coalesce(func.json_array_length(ReviewResult.findings["findings"]), 0)
DASHBOARD_CACHE_TTL_SECONDS = 30
_dashboard_cache: dict[int, tuple[float, "DashboardStatsResponse"]] = {}
_dashboard_cache_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Lifespan: create tables on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CodeReview-Agent",
    description="Multi-agent AI code review service",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def add_timing_headers(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
    response.headers["Server-Timing"] = f'app;dur={elapsed_ms:.2f}'
    return response


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ReviewRequest(BaseModel):
    pr_url: AnyHttpUrl = Field(..., description="Full URL of the GitHub Pull Request")


class ReviewCreateResponse(BaseModel):
    task_id: int
    status: str
    message: str


class AgentResultSchema(BaseModel):
    agent_name: str
    findings: dict[str, Any]
    confidence: float


class ReportSchema(BaseModel):
    final_report: str
    markdown_report: str
    created_at: str


class ReviewStatusResponse(BaseModel):
    task_id: int
    pr_url: str
    status: str
    created_at: str
    updated_at: str
    results: list[AgentResultSchema] = []
    report: ReportSchema | None = None


class HealthResponse(BaseModel):
    status: str
    version: str


class RecentReviewItem(BaseModel):
    task_id: int
    pr_url: str
    status: str
    created_at: str
    updated_at: str
    findings_count: int
    has_report: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _enqueue_review(pr_url: str, db: AsyncSession) -> ReviewTask:
    """Create a pending review task and launch the background orchestrator."""
    task = ReviewTask(pr_url=pr_url, status=TaskStatus.PENDING)
    db.add(task)
    await db.flush()
    await db.refresh(task)
    await set_task_status(task.id, TaskStatus.PENDING.value)
    await db.commit()
    _dashboard_cache.clear()

    orchestrator = Orchestrator()
    asyncio.create_task(orchestrator.run(task_id=task.id, pr_url=pr_url))
    return task


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post(
    "/review",
    response_model=ReviewCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a PR for code review",
)
async def create_review(
    payload: ReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> ReviewCreateResponse:
    """
    Accept a Pull Request URL and create a new review task.

    Returns the **task_id** which can be used to poll for results.
    """
    task = await _enqueue_review(str(payload.pr_url), db)

    return ReviewCreateResponse(
        task_id=task.id,
        status=task.status.value,
        message="Review task created. Poll GET /review/{task_id} for updates.",
    )


@app.get(
    "/review/{task_id}",
    response_model=ReviewStatusResponse,
    summary="Get review task status and results",
)
async def get_review(
    task_id: int,
    include_results: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
) -> ReviewStatusResponse:
    """
    Return the current status of a review task.

    Once the task reaches **completed** status the response also includes
    per-agent findings and the aggregated final report.
    """
    options = [selectinload(ReviewTask.report)]
    if include_results:
        options.append(selectinload(ReviewTask.results))

    stmt = (
        select(ReviewTask)
        .where(ReviewTask.id == task_id)
        .options(*options)
    )
    result = await db.execute(stmt)
    task: ReviewTask | None = result.scalar_one_or_none()

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review task {task_id} not found.",
        )

    # Prefer Redis for status (fast path); fall back to DB value
    cached_status = await get_task_status(task_id)
    current_status = cached_status if cached_status is not None else task.status.value

    results = []
    if include_results:
        results = [
            AgentResultSchema(
                agent_name=r.agent_name,
                findings=r.findings,
                confidence=r.confidence,
            )
            for r in task.results
        ]

    report_schema: ReportSchema | None = None
    if task.report is not None:
        report_schema = ReportSchema(
            final_report=task.report.final_report,
            markdown_report=task.report.markdown_report,
            created_at=task.report.created_at.isoformat(),
        )

    return ReviewStatusResponse(
        task_id=task.id,
        pr_url=task.pr_url,
        status=current_status,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        results=results,
        report=report_schema,
    )


@app.get(
    "/reviews/recent",
    response_model=list[RecentReviewItem],
    summary="List recent review tasks",
)
async def list_recent_reviews(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> list[RecentReviewItem]:
    """Return recent review tasks with lightweight summary fields."""
    return await _build_recent_review_items(limit=limit, db=db)


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
)
async def health_check() -> HealthResponse:
    """Return a simple liveness probe response."""
    return HealthResponse(status="ok", version=app.version)


# ---------------------------------------------------------------------------
# GitHub Webhook
# ---------------------------------------------------------------------------

@app.post(
    "/webhook/github",
    summary="Receive GitHub PR events and auto-trigger review",
    status_code=status.HTTP_202_ACCEPTED,
)
async def github_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Handle GitHub ``pull_request`` webhook events.

    Verifies the HMAC-SHA256 signature when GITHUB_WEBHOOK_SECRET is set,
    then triggers a review for ``opened`` and ``synchronize`` actions.
    """
    body = await request.body()

    # --- Signature verification -------------------------------------------
    secret = settings.GITHUB_WEBHOOK_SECRET
    if secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    # --- Parse payload ----------------------------------------------------
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    event = request.headers.get("X-GitHub-Event", "")
    if event != "pull_request":
        return {"ignored": True, "reason": f"event '{event}' not handled"}

    action = payload.get("action", "")
    if action not in {"opened", "synchronize", "reopened"}:
        return {"ignored": True, "reason": f"action '{action}' not handled"}

    pr_url = payload.get("pull_request", {}).get("html_url")
    if not pr_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing pull_request.html_url")

    # --- Create review task (reuse same logic as POST /review) ------------
    task = await _enqueue_review(pr_url, db)

    return {"task_id": task.id, "pr_url": pr_url, "status": "queued"}


# ---------------------------------------------------------------------------
# Stats / Dashboard API
# ---------------------------------------------------------------------------

class SeverityCount(BaseModel):
    severity: str
    count: int


class StatsSummaryResponse(BaseModel):
    total_tasks: int
    completed: int
    failed: int
    total_findings: int
    by_severity: list[SeverityCount]


class TrendPoint(BaseModel):
    date: str       # YYYY-MM-DD
    count: int


class TrendResponse(BaseModel):
    tasks: list[TrendPoint]
    findings: list[TrendPoint]


class CategoryCount(BaseModel):
    category: str
    count: int


class DashboardStatsResponse(BaseModel):
    summary: StatsSummaryResponse
    top_categories: list[CategoryCount]
    trends: TrendResponse


async def _build_recent_review_items(limit: int, db: AsyncSession) -> list[RecentReviewItem]:
    findings_subquery = (
        select(
            ReviewResult.task_id.label("task_id"),
            func.coalesce(func.sum(FINDINGS_COUNT_EXPR), 0).label("findings_count"),
        )
        .group_by(ReviewResult.task_id)
        .subquery()
    )
    report_subquery = (
        select(
            ReviewReport.task_id.label("task_id"),
            func.count(ReviewReport.id).label("report_count"),
        )
        .group_by(ReviewReport.task_id)
        .subquery()
    )
    stmt = (
        select(
            ReviewTask.id,
            ReviewTask.pr_url,
            ReviewTask.status,
            ReviewTask.created_at,
            ReviewTask.updated_at,
            func.coalesce(findings_subquery.c.findings_count, 0).label("findings_count"),
            (func.coalesce(report_subquery.c.report_count, 0) > 0).label("has_report"),
        )
        .outerjoin(findings_subquery, findings_subquery.c.task_id == ReviewTask.id)
        .outerjoin(report_subquery, report_subquery.c.task_id == ReviewTask.id)
        .order_by(ReviewTask.created_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    rows = (await db.execute(stmt)).all()
    return [
        RecentReviewItem(
            task_id=row.id,
            pr_url=row.pr_url,
            status=row.status.value,
            created_at=row.created_at.isoformat(),
            updated_at=row.updated_at.isoformat(),
            findings_count=int(row.findings_count or 0),
            has_report=bool(row.has_report),
        )
        for row in rows
    ]


async def _build_stats_summary(db: AsyncSession) -> StatsSummaryResponse:
    counts_stmt = select(
        func.count(ReviewTask.id).label("total_tasks"),
        func.count(ReviewTask.id).filter(ReviewTask.status == TaskStatus.COMPLETED).label("completed"),
        func.count(ReviewTask.id).filter(ReviewTask.status == TaskStatus.FAILED).label("failed"),
    )
    counts = (await db.execute(counts_stmt)).one()
    total_findings = await db.scalar(select(func.coalesce(func.sum(FINDINGS_COUNT_EXPR), 0)).select_from(ReviewResult))

    finding_elements = func.json_array_elements(ReviewResult.findings["findings"]).table_valued("value").lateral()
    severity = finding_elements.c.value.op("->>")("severity")
    severity_stmt = (
        select(severity.label("severity"), func.count().label("count"))
        .select_from(ReviewResult)
        .join(finding_elements, true())
        .group_by(severity)
    )
    severity_rows = (await db.execute(severity_stmt)).all()
    by_severity = [
        SeverityCount(severity=row.severity or "UNKNOWN", count=row.count)
        for row in severity_rows
    ]

    return StatsSummaryResponse(
        total_tasks=counts.total_tasks or 0,
        completed=counts.completed or 0,
        failed=counts.failed or 0,
        total_findings=total_findings or 0,
        by_severity=by_severity,
    )


async def _build_top_categories(limit: int, db: AsyncSession) -> list[CategoryCount]:
    finding_elements = func.json_array_elements(ReviewResult.findings["findings"]).table_valued("value").lateral()
    category = finding_elements.c.value.op("->>")("category")
    stmt = (
        select(category.label("category"), func.count().label("count"))
        .select_from(ReviewResult)
        .join(finding_elements, true())
        .group_by(category)
        .order_by(func.count().desc(), category.asc())
        .limit(max(1, min(limit, 25)))
    )
    rows = (await db.execute(stmt)).all()
    return [
        CategoryCount(category=row.category or "unknown", count=row.count)
        for row in rows
    ]


async def _build_trends(days: int, db: AsyncSession) -> TrendResponse:
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    task_stmt = (
        select(
            func.date(ReviewTask.created_at).label("day"),
            func.count(ReviewTask.id).label("cnt"),
        )
        .where(ReviewTask.created_at >= cutoff)
        .group_by(func.date(ReviewTask.created_at))
        .order_by(func.date(ReviewTask.created_at))
    )
    task_rows = (await db.execute(task_stmt)).all()

    findings_stmt = (
        select(
            func.date(ReviewTask.created_at).label("day"),
            func.coalesce(func.sum(FINDINGS_COUNT_EXPR), 0).label("cnt"),
        )
        .select_from(ReviewResult)
        .join(ReviewTask, ReviewTask.id == ReviewResult.task_id)
        .where(ReviewTask.created_at >= cutoff)
        .group_by(func.date(ReviewTask.created_at))
        .order_by(func.date(ReviewTask.created_at))
    )
    finding_rows = (await db.execute(findings_stmt)).all()

    return TrendResponse(
        tasks=[TrendPoint(date=str(row.day), count=row.cnt) for row in task_rows],
        findings=[TrendPoint(date=str(row.day), count=row.cnt) for row in finding_rows],
    )


async def _get_dashboard_snapshot(days: int, db: AsyncSession) -> DashboardStatsResponse:
    now = time.monotonic()
    cached = _dashboard_cache.get(days)
    if cached and cached[0] > now:
        return cached[1]

    async with _dashboard_cache_lock:
        cached = _dashboard_cache.get(days)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        payload = DashboardStatsResponse(
            summary=await _build_stats_summary(db),
            top_categories=await _build_top_categories(limit=10, db=db),
            trends=await _build_trends(days=days, db=db),
        )
        _dashboard_cache[days] = (time.monotonic() + DASHBOARD_CACHE_TTL_SECONDS, payload)
        return payload


@app.get(
    "/stats/summary",
    response_model=StatsSummaryResponse,
    summary="Overall review statistics",
)
async def stats_summary(db: AsyncSession = Depends(get_db)) -> StatsSummaryResponse:
    """Return aggregate counts across all review tasks."""
    return await _build_stats_summary(db)


@app.get(
    "/stats/top_categories",
    response_model=list[CategoryCount],
    summary="Top finding categories across all reviews",
)
async def stats_top_categories(
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
) -> list[CategoryCount]:
    """Return the most frequently occurring finding categories."""
    return await _build_top_categories(limit=limit, db=db)


@app.get(
    "/stats/trends",
    response_model=TrendResponse,
    summary="Daily task and finding counts over past N days",
)
async def stats_trends(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
) -> TrendResponse:
    """Return per-day counts of submitted tasks and total findings."""
    return await _build_trends(days=days, db=db)


@app.get(
    "/stats/dashboard",
    response_model=DashboardStatsResponse,
    summary="Combined dashboard payload",
)
async def stats_dashboard(
    days: int = Query(default=30, ge=7, le=90),
    db: AsyncSession = Depends(get_db),
) -> DashboardStatsResponse:
    """Return the dashboard payload in a single request with short-lived caching."""
    return await _get_dashboard_snapshot(days=days, db=db)
