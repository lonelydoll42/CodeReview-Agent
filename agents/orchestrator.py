"""Orchestrator – pulls a PR diff and runs all review agents in parallel."""
from __future__ import annotations

import asyncio
import logging
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from agents.aggregator import Aggregator, AggregatedReport
from agents.base import AgentResult, FileDiff
from agents.logic_agent import LogicAgent
from agents.performance_agent import PerformanceAgent
from agents.security_agent import SecurityAgent
from agents.style_agent import StyleAgent
from config import settings
from storage.cache import (
    get_dedup_task_id,
    set_agent_result,
    set_dedup_task_id,
    set_task_status,
)
from notifications.webhook import notify_review_complete, notify_review_failed
from storage.models import (
    AsyncSessionLocal,
    ReviewReport,
    ReviewResult,
    ReviewTask,
    TaskStatus,
)
from tools.github_client import GitHubClient
from tools.review_version import review_cache_key

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = {
    "python", "javascript", "typescript", "go", "java",
    "ruby", "rust", "cpp", "c", "csharp", "php", "swift",
    "kotlin", "scala", "bash", "sql",
}


async def _run_one_agent(
    agent: StyleAgent | SecurityAgent | LogicAgent | PerformanceAgent,
    file_diff: FileDiff,
    task_id: int,
    timeout: int = 30,
) -> AgentResult | None:
    """Run a single agent on a single file diff with timeout and error handling."""
    agent_name = type(agent).__name__
    try:
        result = await asyncio.wait_for(
            agent.review(file_diff),
            timeout=timeout,
        )
        await set_agent_result(task_id, agent_name, result.model_dump())
        return result
    except asyncio.TimeoutError:
        logger.warning(
            "[task=%s] %s timed out on %s – skipping",
            task_id, agent_name, file_diff.filename,
        )
    except Exception as exc:
        logger.warning(
            "[task=%s] %s raised %s on %s – skipping",
            task_id, agent_name, exc, file_diff.filename,
        )
    return None


class Orchestrator:
    """Coordinates diff fetching, parallel agent execution, aggregation, and persistence."""

    def __init__(self) -> None:
        api_key = settings.ANTHROPIC_API_KEY
        self.agents = [
            StyleAgent(api_key=api_key),
            SecurityAgent(api_key=api_key),
            LogicAgent(api_key=api_key),
            PerformanceAgent(api_key=api_key),
        ]
        self.aggregator = Aggregator(api_key=api_key)
        self.github = GitHubClient()

    async def run(self, task_id: int, pr_url: str) -> None:
        """Main execution flow, started via asyncio.create_task()."""
        await set_task_status(task_id, TaskStatus.RUNNING.value)

        # --- 1. Fetch diff + metadata ------------------------------------
        try:
            pr_diff = await asyncio.to_thread(self.github.get_pr_diff, pr_url)
        except Exception as exc:
            logger.error("[task=%s] Failed to fetch PR diff: %s", task_id, exc)
            await self._fail(task_id, str(exc))
            await notify_review_failed(pr_url, task_id, str(exc))
            return

        pr_metadata = pr_diff.metadata
        commit_sha = pr_diff.head_sha
        cache_key = review_cache_key(
            pr_diff.base_sha, commit_sha, pr_diff.merge_base_sha, settings.REVIEW_RULESET_VERSION,
        )
        if settings.ENABLE_DEDUP_CACHE:
            cached_id = await get_dedup_task_id(pr_url, cache_key)
            if cached_id is not None and cached_id != task_id:
                if await self._reuse_report(cached_id, task_id, pr_url):
                    return

        # --- 2. Filter to supported languages ----------------------------
        file_diffs: List[FileDiff] = [
            FileDiff(
                filename=f.filename,
                language=f.language,
                added_lines=f.added_lines,
                removed_lines=f.removed_lines,
                raw_diff=getattr(f, "patch", ""),
                full_source=f.full_source,
                status=f.status,
                previous_filename=f.previous_filename,
            )
            for f in pr_diff.files
            if f.language in SUPPORTED_LANGUAGES
        ]

        if not file_diffs:
            logger.info("[task=%s] No supported-language files in diff", task_id)
            empty = self.aggregator.aggregate([], pr_url=pr_url, task_id=task_id, pr_metadata=pr_metadata)
            await self._persist(task_id, [], empty)
            return

        # --- 3. Dispatch all agent × file tasks --------------------------
        tasks = [
            _run_one_agent(agent, fd, task_id, settings.AGENT_TIMEOUT_SECONDS)
            for agent in self.agents
            for fd in file_diffs
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        agent_results: List[AgentResult] = [
            r for r in raw_results
            if isinstance(r, AgentResult)
        ]

        # --- 4. Aggregate ------------------------------------------------
        report: AggregatedReport = self.aggregator.aggregate(
            agent_results,
            pr_url=pr_url,
            task_id=task_id,
            pr_metadata=pr_metadata,
        )

        # --- 6. Persist --------------------------------------------------
        await self._persist(task_id, agent_results, report)

        # Only publish cache entries after a durable, complete result exists.
        if settings.ENABLE_DEDUP_CACHE and len(agent_results) == len(tasks):
            await set_dedup_task_id(pr_url, cache_key, task_id)

        # --- 7. Post review comment to PR (top-level) ------------------
        if settings.ENABLE_PR_COMMENT:
            body = f"Reviewed commit `{commit_sha}`.\n\n{report.markdown_report}"
            ok = await asyncio.to_thread(self.github.post_review_comment, pr_url, body)
            if not ok:
                logger.warning("[task=%s] Failed to post top-level PR comment", task_id)

        # --- 8. Post inline review comments ------------------------------
        if settings.ENABLE_INLINE_COMMENT and report.findings:
            findings_dicts = [f.model_dump() for f in report.findings]
            ok = await asyncio.to_thread(
                self.github.post_inline_review,
                pr_url,
                findings_dicts,
                summary_body=report.executive_summary[:500],
                commit_sha=commit_sha,
                file_diffs=file_diffs,
            )
            if not ok:
                logger.warning("[task=%s] Failed to post inline review", task_id)

        # --- 9. Notify ---------------------------------------------------
        await notify_review_complete(
            pr_url=pr_url,
            task_id=task_id,
            stats=report.stats,
            executive_summary=report.executive_summary,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _reuse_report(self, cached_id: int, task_id: int, pr_url: str) -> bool:
        """Copy a durable cached report so the new task is actually queryable."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ReviewTask).where(ReviewTask.id == cached_id).options(
                    selectinload(ReviewTask.report), selectinload(ReviewTask.results),
                )
            )
            cached = result.scalar_one_or_none()
            if cached is None or cached.status != TaskStatus.COMPLETED or cached.report is None or cached.pr_url != pr_url:
                return False
            try:
                report = AggregatedReport.model_validate_json(cached.report.final_report)
                results = [AgentResult.model_validate(row.findings) for row in cached.results]
            except ValueError:
                return False
            report.task_id = task_id
        await self._persist(task_id, results, report)
        return True

    async def _fail(self, task_id: int, error: str) -> None:
        await set_task_status(task_id, TaskStatus.FAILED.value)
        async with AsyncSessionLocal() as session:
            task = await session.get(ReviewTask, task_id)
            if task:
                task.status = TaskStatus.FAILED
                await session.commit()

    async def _persist(
        self,
        task_id: int,
        agent_results: List[AgentResult],
        report: AggregatedReport,
    ) -> None:
        async with AsyncSessionLocal() as session:
            # Write one ReviewResult per AgentResult
            for ar in agent_results:
                avg_conf = (
                    sum(f.confidence for f in ar.findings) / len(ar.findings)
                    if ar.findings else 0.0
                )
                session.add(
                    ReviewResult(
                        task_id=task_id,
                        agent_name=ar.agent_name,
                        findings=ar.model_dump(),
                        confidence=avg_conf,
                    )
                )

            # Write the aggregated report
            session.add(
                ReviewReport(
                    task_id=task_id,
                    final_report=report.model_dump_json(),
                    markdown_report=report.markdown_report,
                )
            )

            # Mark task complete
            task = await session.get(ReviewTask, task_id)
            if task:
                task.status = TaskStatus.COMPLETED

            await session.commit()

        await set_task_status(task_id, TaskStatus.COMPLETED.value)
