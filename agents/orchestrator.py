"""Orchestrator – pulls a PR diff and runs all review agents in parallel."""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import weakref
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from agents.aggregator import Aggregator, AggregatedReport
from agents.base import AgentExecution, AgentResult, FileCoverage, FileDiff
from agents.errors import AgentRuntimeError
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
    ReviewCoverage,
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

_DEFAULT_MAX_PARALLEL_AGENTS = 5
_agent_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = weakref.WeakKeyDictionary()


def _agent_limit() -> int:
    """Read and sanitize the process configuration used by the limiter."""
    try:
        configured = int(getattr(settings, "MAX_PARALLEL_AGENTS", _DEFAULT_MAX_PARALLEL_AGENTS))
    except (TypeError, ValueError):
        configured = _DEFAULT_MAX_PARALLEL_AGENTS
    return max(1, configured)


def _get_agent_limiter() -> asyncio.Semaphore:
    """Return one shared limiter for all reviews on the current event loop."""
    loop = asyncio.get_running_loop()
    limit = _agent_limit()
    existing = _agent_limiters.get(loop)
    if existing is None or existing[0] != limit:
        # Configuration is normally immutable for a process lifetime.  A
        # replacement is useful for tests and only occurs before a new batch
        # starts; running tasks retain the semaphore they already acquired.
        limiter = asyncio.Semaphore(limit)
        _agent_limiters[loop] = (limit, limiter)
        return limiter
    return existing[1]


def _error_code(exc: BaseException) -> str:
    """Map provider/runtime errors to a stable, non-sensitive public code."""
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,64}", reason):
        return "agent_timeout" if reason == "provider_request_timeout" else reason
    name = type(exc).__name__.lower()
    if "malformed" in name or "response" in name:
        return "malformed_response"
    if "timeout" in name:
        return "agent_timeout"
    return "agent_error"


async def _cache_agent_attempt(
    task_id: int,
    agent_name: str,
    filename: str,
    payload: dict,
) -> None:
    """Cache an attempt without making Redis availability affect analysis."""
    try:
        await set_agent_result(task_id, agent_name, payload, filename=filename)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[task=%s] Redis cache write failed for %s/%s (%s)",
            task_id,
            agent_name,
            filename,
            type(exc).__name__,
        )


async def _run_one_agent(
    agent: StyleAgent | SecurityAgent | LogicAgent | PerformanceAgent,
    file_diff: FileDiff,
    task_id: int,
    timeout: int = 30,
    execution_records: list[AgentExecution] | None = None,
) -> AgentResult | None:
    """Run one attempt under the shared limit and record its terminal state.

    The semaphore is acquired *before* ``wait_for`` starts, so queue time is
    not charged against the per-agent execution timeout.  ``async with``
    guarantees release on timeout, cancellation, and provider failure.
    """
    agent_name = type(agent).__name__
    async with _get_agent_limiter():
        # Queueing for a shared permit is scheduling overhead, not agent
        # execution time.  Start the duration clock after the permit is held.
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                agent.review(file_diff),
                timeout=timeout,
            )
            if not isinstance(result, AgentResult):
                raise AgentRuntimeError(
                    "malformed_agent_result",
                    "agent returned an invalid result object",
                    agent_name=agent_name,
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            duration = time.perf_counter() - started
            execution = AgentExecution(
                agent_name=agent_name,
                filename=file_diff.filename,
                language=file_diff.language,
                status="timeout",
                error_code="agent_timeout",
                duration_seconds=duration,
            )
            if execution_records is not None:
                execution_records.append(execution)
            await _cache_agent_attempt(
                task_id,
                agent_name,
                file_diff.filename,
                {
                    "agent_name": agent_name,
                    "filename": file_diff.filename,
                    "status": "timeout",
                    "findings": [],
                    "error_code": "agent_timeout",
                },
            )
            logger.warning(
                "[task=%s] %s timed out on %s – recorded as incomplete",
                task_id, agent_name, file_diff.filename,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            duration = time.perf_counter() - started
            code = _error_code(exc)
            attempt_status = "timeout" if code == "agent_timeout" else "failed"
            execution = AgentExecution(
                agent_name=agent_name,
                filename=file_diff.filename,
                language=file_diff.language,
                status=attempt_status,
                error_code=code,
                duration_seconds=duration,
            )
            if execution_records is not None:
                execution_records.append(execution)
            await _cache_agent_attempt(
                task_id,
                agent_name,
                file_diff.filename,
                {
                    "agent_name": agent_name,
                    "filename": file_diff.filename,
                    "status": attempt_status,
                    "findings": [],
                    "error_code": code,
                },
            )
            logger.warning(
                "[task=%s] %s failed on %s (%s)",
                task_id, agent_name, file_diff.filename, code,
            )
            return None
        else:
            duration = time.perf_counter() - started
            execution = AgentExecution(
                agent_name=agent_name,
                filename=file_diff.filename,
                language=file_diff.language,
                status="completed",
                duration_seconds=duration,
                result=result,
            )
            if execution_records is not None:
                execution_records.append(execution)
            await _cache_agent_attempt(
                task_id,
                agent_name,
                file_diff.filename,
                {
                    **result.model_dump(),
                    "status": "completed",
                    "filename": file_diff.filename,
                },
            )
            return result


class Orchestrator:
    """Coordinates diff fetching, parallel agent execution, aggregation, and persistence."""

    def __init__(self) -> None:
        api_key = settings.ANTHROPIC_API_KEY
        runtime_options = {
            "request_timeout": settings.ANTHROPIC_REQUEST_TIMEOUT_SECONDS,
            "max_retries": settings.ANTHROPIC_MAX_RETRIES,
        }
        self.agents = [
            StyleAgent(api_key=api_key, **runtime_options),
            SecurityAgent(api_key=api_key, **runtime_options),
            LogicAgent(api_key=api_key, **runtime_options),
            PerformanceAgent(api_key=api_key, **runtime_options),
        ]
        self.aggregator = Aggregator(api_key=api_key)
        self.github = GitHubClient()

    async def run(self, task_id: int, pr_url: str) -> None:
        """Run one review and close provider clients on every exit path."""
        try:
            await self._run(task_id, pr_url)
        finally:
            await self.aclose()

    async def _run(self, task_id: int, pr_url: str) -> None:
        """Main execution flow, started via asyncio.create_task()."""
        cache_key = ""
        commit_sha = ""
        file_diffs: List[FileDiff] = []
        execution_records: list[AgentExecution] = []
        coverage: list[FileCoverage] = []
        review_status = TaskStatus.FAILED

        try:
            await self._mark_running(task_id)
            # --- 1. Fetch diff + metadata --------------------------------
            pr_diff = await asyncio.to_thread(self.github.get_pr_diff, pr_url)
            pr_metadata = pr_diff.metadata
            commit_sha = pr_diff.head_sha
            cache_key = review_cache_key(
                pr_diff.base_sha,
                commit_sha,
                pr_diff.merge_base_sha,
                settings.REVIEW_RULESET_VERSION,
            )
            if settings.ENABLE_DEDUP_CACHE:
                cached_id = await get_dedup_task_id(pr_url, cache_key)
                if cached_id is not None and cached_id != task_id:
                    if await self._reuse_report(cached_id, task_id, pr_url):
                        return

            # --- 2. Filter to supported languages ------------------------
            file_diffs = [
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
            coverage = self._initial_coverage(pr_diff, file_diffs)

            if not file_diffs:
                logger.info("[task=%s] No supported-language files in diff", task_id)
                review_status = TaskStatus.UNCOVERED
                report = await self._aggregate(
                    [],
                    pr_url=pr_url,
                    task_id=task_id,
                    pr_metadata=pr_metadata,
                    review_status=review_status.value,
                    coverage=coverage,
                    agent_executions=execution_records,
                )
                self._annotate_report(report, review_status, coverage, execution_records)
                await self._persist(task_id, [], report)
                return

            # --- 3. Dispatch all agent × file tasks ----------------------
            attempt_specs = [
                (agent, fd)
                for agent in self.agents
                for fd in file_diffs
            ]
            tasks = [
                _run_one_agent(
                    agent,
                    fd,
                    task_id,
                    _timeout_seconds(),
                    execution_records,
                )
                for agent, fd in attempt_specs
            ]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            agent_results = [
                result for result in raw_results if isinstance(result, AgentResult)
            ]
            # A defensive gather guard: _run_one_agent records expected errors,
            # but a task wrapper failure must still count against coverage.
            recorded_keys = {
                (item.agent_name, item.filename)
                for item in execution_records
            }
            for index, result in enumerate(raw_results):
                if isinstance(result, BaseException):
                    agent, file_diff = attempt_specs[index]
                    key = (type(agent).__name__, file_diff.filename)
                    if key not in recorded_keys:
                        execution_records.append(
                            AgentExecution(
                                agent_name=key[0],
                                filename=file_diff.filename,
                                language=file_diff.language,
                                status="failed",
                                error_code=_error_code(result),
                            )
                        )
                        recorded_keys.add(key)
                    logger.warning(
                        "[task=%s] agent task wrapper failed (%s)",
                        task_id,
                        type(result).__name__,
                    )

            coverage = self._finalize_coverage(coverage, execution_records)
            total_attempts = len(tasks)
            if total_attempts and len(agent_results) == total_attempts:
                review_status = TaskStatus.COMPLETED
            elif agent_results:
                review_status = TaskStatus.PARTIAL
            else:
                # Empty findings from successful agents are valid; zero
                # successful agent executions is a failed review.
                review_status = TaskStatus.FAILED

            # --- 4. Aggregate --------------------------------------------
            report = await self._aggregate(
                agent_results,
                pr_url=pr_url,
                task_id=task_id,
                pr_metadata=pr_metadata,
                review_status=review_status.value,
                coverage=coverage,
                agent_executions=execution_records,
            )
            self._annotate_report(report, review_status, coverage, execution_records)

            # --- 5. Persist ----------------------------------------------
            await self._persist(task_id, agent_results, report)

            # Only publish cache entries after a durable, complete result.
            if settings.ENABLE_DEDUP_CACHE and _cache_eligible(review_status, coverage):
                try:
                    await set_dedup_task_id(pr_url, cache_key, task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[task=%s] Dedup cache write failed (%s); durable report remains authoritative",
                        task_id,
                        type(exc).__name__,
                    )

        except asyncio.CancelledError:
            # Child attempts release their semaphore in ``async with``.  Mark
            # the task terminal before propagating cancellation to the caller.
            await self._fail(task_id, "review_cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[task=%s] Review pipeline failed (%s)", task_id, type(exc).__name__)
            await self._fail(task_id, _error_code(exc))
            try:
                await notify_review_failed(pr_url, task_id, _error_code(exc))
            except Exception:  # noqa: BLE001
                logger.warning("[task=%s] Failed to notify review failure", task_id)
            return

        if review_status == TaskStatus.FAILED:
            try:
                await notify_review_failed(pr_url, task_id, "all_agent_attempts_failed")
            except Exception:  # noqa: BLE001
                logger.warning("[task=%s] Failed to notify all-agent failure", task_id)
            return
        if review_status == TaskStatus.UNCOVERED:
            logger.info("[task=%s] Skipping completion write-back for uncovered review", task_id)
            return

        # External write-back and notification are post-terminal side effects.
        # Their failures must not turn a durably completed/partial report back
        # into a task stuck in RUNNING.
        try:
            inline_lines = {
                fd.filename: {line for line, _ in fd.added_lines}
                for fd in file_diffs
            }
            has_summary_only_findings = any(
                finding.line_start not in inline_lines.get(finding.file, set())
                for finding in report.findings
            )
            if settings.ENABLE_PR_COMMENT or (
                settings.ENABLE_INLINE_COMMENT and has_summary_only_findings
            ):
                body = f"Reviewed commit `{commit_sha}`.\n\n{report.markdown_report}"
                ok = await asyncio.to_thread(self.github.post_review_comment, pr_url, body)
                if not ok:
                    logger.warning("[task=%s] Failed to post top-level PR comment", task_id)

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

            await notify_review_complete(
                pr_url=pr_url,
                task_id=task_id,
                stats=report.stats,
                executive_summary=report.executive_summary,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[task=%s] Post-review side effect failed (%s); terminal DB status preserved",
                task_id,
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _mark_running(self, task_id: int) -> None:
        """Write RUNNING to Redis and the durable task row together."""
        try:
            await set_task_status(task_id, TaskStatus.RUNNING.value)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[task=%s] Redis RUNNING status write failed (%s)",
                task_id,
                type(exc).__name__,
            )
        async with AsyncSessionLocal() as session:
            task = await session.get(ReviewTask, task_id)
            if task:
                task.status = TaskStatus.RUNNING
                await session.commit()

    async def aclose(self) -> None:
        """Close sync/async provider clients owned by this one-shot runner."""
        clients = []
        for component in [*getattr(self, "agents", []), getattr(self, "aggregator", None)]:
            component_close = getattr(component, "aclose", None)
            if inspect.iscoroutinefunction(component_close):
                try:
                    await component_close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Provider component close failed (%s)", type(exc).__name__)
                continue
            client = getattr(component, "_client", None)
            if client is not None and all(client is not seen for seen in clients):
                clients.append(client)
        for client in clients:
            for method_name in ("aclose", "close"):
                method = getattr(client, method_name, None)
                if method is None or not callable(method):
                    continue
                try:
                    if inspect.iscoroutinefunction(method):
                        await method()
                    else:
                        result = await asyncio.to_thread(method)
                        if inspect.isawaitable(result):
                            await result
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Provider client close failed (%s)", type(exc).__name__)
                break

    async def _aggregate(self, agent_results: List[AgentResult], **kwargs) -> AggregatedReport:
        """Use the async production path while accepting legacy aggregators."""
        aggregate_async = getattr(self.aggregator, "aggregate_async", None)
        if inspect.iscoroutinefunction(aggregate_async):
            return await aggregate_async(agent_results, **kwargs)
        # Test doubles and third-party aggregators may only expose the old
        # synchronous method.  Keep their API while avoiding event-loop
        # blocking in the service path.
        return await asyncio.to_thread(self.aggregator.aggregate, agent_results, **kwargs)

    @staticmethod
    def _annotate_report(
        report: AggregatedReport,
        status: TaskStatus,
        coverage: list[FileCoverage],
        executions: list[AgentExecution],
    ) -> None:
        """Keep report metadata truthful even for legacy aggregator doubles."""
        if isinstance(report, AggregatedReport):
            report.review_status = status.value
            report.coverage = coverage
            report.agent_executions = executions
            report.stats.setdefault("review_status", status.value)
            report.stats.setdefault("coverage", [item.model_dump() for item in coverage])
            report.stats.setdefault(
                "agent_failures",
                [
                    item.model_dump()
                    for item in executions
                    if item.status in {"failed", "timeout"}
                ],
            )
            return
        # MagicMock/legacy report objects are only used by compatibility tests;
        # assigning fields still lets persistence and side effects proceed.
        report.review_status = status.value
        report.coverage = coverage
        report.agent_executions = executions

    @staticmethod
    def _initial_coverage(pr_diff, file_diffs: list[FileDiff]) -> list[FileCoverage]:
        """Create coverage rows for analyzable and skipped changed files."""
        supported_names = {item.filename for item in file_diffs}
        records: list[FileCoverage] = [
            FileCoverage(
                filename=item.filename,
                language=item.language,
                status="partial",
                expected_agents=0,
            )
            for item in file_diffs
        ]
        for item in getattr(pr_diff, "files", []):
            filename = getattr(item, "filename", "")
            if filename in supported_names:
                continue
            language = getattr(item, "language", "") or "unknown"
            records.append(
                FileCoverage(
                    filename=filename,
                    language=language,
                    status="unsupported",
                    reason="unsupported language",
                )
            )
        for filename, reason in getattr(pr_diff, "skipped_files", {}).items():
            if any(item.filename == filename for item in records):
                continue
            status = "unsupported" if "unsupported" in reason.lower() else "skipped"
            records.append(
                FileCoverage(filename=filename, status=status, reason=reason)
            )
        return records

    def _finalize_coverage(
        self,
        coverage: list[FileCoverage],
        executions: list[AgentExecution],
    ) -> list[FileCoverage]:
        """Fill per-file agent counts after all attempts finish."""
        expected = len(self.agents)
        completed_by_file: dict[str, int] = {}
        failed_by_file: dict[str, int] = {}
        language_by_file: dict[str, str] = {}
        for execution in executions:
            language_by_file[execution.filename] = execution.language
            if execution.status == "completed":
                completed_by_file[execution.filename] = completed_by_file.get(execution.filename, 0) + 1
            else:
                failed_by_file[execution.filename] = failed_by_file.get(execution.filename, 0) + 1

        finalized: list[FileCoverage] = []
        for item in coverage:
            if item.status in {"unsupported", "skipped"}:
                finalized.append(item)
                continue
            completed = completed_by_file.get(item.filename, 0)
            failed = failed_by_file.get(item.filename, 0)
            if expected and completed == expected and failed == 0:
                status = "covered"
            elif completed:
                status = "partial"
            else:
                status = "failed"
            finalized.append(
                item.model_copy(
                    update={
                        "language": item.language or language_by_file.get(item.filename, ""),
                        "status": status,
                        "expected_agents": expected,
                        "completed_agents": completed,
                        "failed_agents": failed,
                    }
                )
            )
        return finalized

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
            if any(
                getattr(row, "status", "completed") != "completed"
                for row in cached.results
            ):
                return False
            try:
                report = AggregatedReport.model_validate_json(cached.report.final_report)
                results = [AgentResult.model_validate(row.findings) for row in cached.results]
            except ValueError:
                return False
            # Legacy reports predate durable coverage and remain readable. A
            # new report with explicit skipped/partial coverage is never a
            # valid dedup source.
            if report.coverage and not _cache_eligible(TaskStatus.COMPLETED, report.coverage):
                return False
            report.task_id = task_id
        await self._persist(task_id, results, report)
        return True

    async def _fail(self, task_id: int, error: str) -> None:
        try:
            await set_task_status(task_id, TaskStatus.FAILED.value)
        except Exception:  # noqa: BLE001
            logger.warning("[task=%s] Failed to update Redis failure status", task_id)
        try:
            async with AsyncSessionLocal() as session:
                task = await session.get(ReviewTask, task_id)
                if task:
                    task.status = TaskStatus.FAILED
                    await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[task=%s] Failed to persist terminal failure (%s)",
                task_id,
                type(exc).__name__,
            )

    async def _persist(
        self,
        task_id: int,
        agent_results: List[AgentResult],
        report: AggregatedReport,
        *,
        status: TaskStatus | None = None,
        executions: list[AgentExecution] | None = None,
        coverage: list[FileCoverage] | None = None,
    ) -> None:
        if status is None:
            report_status = getattr(report, "review_status", TaskStatus.COMPLETED.value)
            status = TaskStatus(report_status) if isinstance(report_status, str) else TaskStatus.COMPLETED
        execution_records = executions
        if execution_records is None:
            maybe_records = getattr(report, "agent_executions", None)
            execution_records = maybe_records if isinstance(maybe_records, list) else []
        coverage_records = coverage
        if coverage_records is None:
            maybe_coverage = getattr(report, "coverage", None)
            coverage_records = maybe_coverage if isinstance(maybe_coverage, list) else []

        async with AsyncSessionLocal() as session:
            # Write one ReviewResult per per-file attempt.  Legacy callers
            # that do not provide execution records still get one completed
            # row per AgentResult.
            if execution_records:
                attempts = []
                for execution in execution_records:
                    matching = execution.result or next(
                        (
                            result for result in agent_results
                            if result.agent_name == execution.agent_name
                            and any(finding.file == execution.filename for finding in result.findings)
                        ),
                        None,
                    )
                    attempts.append((execution, matching))
            else:
                attempts = [
                    (
                        AgentExecution(
                            agent_name=ar.agent_name,
                            filename=next((finding.file for finding in ar.findings), ""),
                            status="completed",
                        ),
                        ar,
                    )
                    for ar in agent_results
                ]

            for execution, ar in attempts:
                avg_conf = (
                    sum(f.confidence for f in ar.findings) / len(ar.findings)
                    if ar and ar.findings else 0.0
                )
                findings_payload = ar.model_dump() if ar else {
                    "agent_name": execution.agent_name,
                    "findings": [],
                    "summary": "",
                    "execution_time": execution.duration_seconds or 0.0,
                    "token_used": 0,
                }
                session.add(
                    ReviewResult(
                        task_id=task_id,
                        agent_name=execution.agent_name,
                        filename=execution.filename or None,
                        status=execution.status,
                        error_code=execution.error_code,
                        findings=findings_payload,
                        confidence=avg_conf,
                    )
                )

            for item in coverage_records:
                session.add(
                    ReviewCoverage(
                        task_id=task_id,
                        filename=item.filename,
                        language=item.language,
                        status=item.status,
                        reason=item.reason,
                        expected_agents=item.expected_agents,
                        completed_agents=item.completed_agents,
                        failed_agents=item.failed_agents,
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

            # Persist the coverage-aware terminal status with the report.
            task = await session.get(ReviewTask, task_id)
            if task:
                task.status = status

            await session.commit()

        try:
            await set_task_status(task_id, status.value)
        except Exception as exc:  # noqa: BLE001
            # The database commit above is authoritative; a Redis outage must
            # not rewrite a durable terminal result as FAILED.
            logger.warning(
                "[task=%s] Redis terminal status write failed (%s)",
                task_id,
                type(exc).__name__,
            )


def _timeout_seconds() -> float:
    """Read the timeout without making test doubles or bad config fatal."""
    try:
        return max(0.001, float(getattr(settings, "AGENT_TIMEOUT_SECONDS", 30)))
    except (TypeError, ValueError):
        return 30.0


def _cache_eligible(status: TaskStatus, coverage: list[FileCoverage]) -> bool:
    """Only cache a persisted review that covered every changed file."""
    return bool(coverage) and status == TaskStatus.COMPLETED and all(
        item.status == "covered" for item in coverage
    )
