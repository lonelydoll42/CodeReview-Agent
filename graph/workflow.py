"""LangGraph state machine for the CodeReview-Agent pipeline.

The graph models the full review workflow:
  fetch_diff → dispatch_agents → [run_style, run_security, run_logic, run_performance]
              → aggregate → save_results → END

Any node that sets state["error"] is routed to error_handler → END.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, TypedDict

from agents.aggregator import Aggregator
from agents.base import AgentResult, FileDiff
from agents.logic_agent import LogicAgent
from agents.performance_agent import PerformanceAgent
from agents.security_agent import SecurityAgent
from agents.style_agent import StyleAgent
from config import settings
from storage.cache import set_agent_result, set_task_status
from storage.models import (
    AsyncSessionLocal,
    ReviewReport,
    ReviewResult,
    ReviewTask,
    TaskStatus,
)
from tools.github_client import GitHubClient

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = {
    "python", "javascript", "typescript", "go", "java",
    "ruby", "rust", "cpp", "c", "csharp", "php", "swift",
    "kotlin", "scala", "bash", "sql",
}

# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------


class ReviewState(TypedDict):
    task_id: int
    pr_url: str
    # Populated by fetch_diff
    file_diffs: List[dict]               # FileDiff.model_dump() list
    pr_metadata: Dict[str, object]       # get_pr_metadata() result
    # Populated by dispatch_agents
    agent_tasks: List[dict]              # {agent: str, file: str}
    # Populated by each agent node
    agent_results: Dict[str, List[dict]] # agent_name → List[AgentResult.model_dump()]
    # Populated by aggregate
    report: Optional[dict]               # AggregatedReport.model_dump()
    # Error info
    error: Optional[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agents() -> Dict[str, StyleAgent | SecurityAgent | LogicAgent | PerformanceAgent]:
    key = settings.ANTHROPIC_API_KEY
    return {
        "StyleAgent":       StyleAgent(api_key=key),
        "SecurityAgent":    SecurityAgent(api_key=key),
        "LogicAgent":       LogicAgent(api_key=key),
        "PerformanceAgent": PerformanceAgent(api_key=key),
    }


def _safe_update(state: ReviewState, updates: dict) -> ReviewState:
    """Return a new state dict with *updates* merged in."""
    return {**state, **updates}


# ---------------------------------------------------------------------------
# Node: fetch_diff
# ---------------------------------------------------------------------------

def fetch_diff(state: ReviewState) -> ReviewState:
    """Call GitHubClient to retrieve the PR diff and populate file_diffs."""
    try:
        client = GitHubClient()
        pr_url = state["pr_url"]
        pr_diff = client.get_pr_diff(pr_url)
        pr_metadata = pr_diff.metadata
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
            ).model_dump()
            for f in pr_diff.files
            if f.language in SUPPORTED_LANGUAGES
        ]
        return _safe_update(state, {"file_diffs": file_diffs, "pr_metadata": pr_metadata, "error": None})
    except Exception as exc:
        logger.error("fetch_diff failed: %s", exc)
        return _safe_update(state, {"error": f"fetch_diff: {exc}"})


# ---------------------------------------------------------------------------
# Node: dispatch_agents
# ---------------------------------------------------------------------------

def dispatch_agents(state: ReviewState) -> ReviewState:
    """Expand file_diffs × agent names into agent_tasks."""
    if state.get("error"):
        return state
    agent_names = ["StyleAgent", "SecurityAgent", "LogicAgent", "PerformanceAgent"]
    tasks = [
        {"agent": agent_name, "file": fd["filename"]}
        for agent_name in agent_names
        for fd in state.get("file_diffs", [])
    ]
    return _safe_update(state, {"agent_tasks": tasks, "agent_results": {}})


# ---------------------------------------------------------------------------
# Node helpers: run a single agent over all its assigned file diffs
# ---------------------------------------------------------------------------

async def _run_agent_node(state: ReviewState, agent_name: str) -> ReviewState:
    """Generic async handler for a single agent's node."""
    if state.get("error"):
        return state

    agents = _make_agents()
    agent = agents[agent_name]
    file_diffs = state.get("file_diffs", [])
    task_id = state["task_id"]
    timeout = settings.AGENT_TIMEOUT_SECONDS

    async def _review_one(fd_dict: dict) -> AgentResult | None:
        fd = FileDiff(**fd_dict)
        try:
            result = await asyncio.wait_for(agent.review(fd), timeout=timeout)
            await set_agent_result(task_id, agent_name, result.model_dump())
            return result
        except asyncio.TimeoutError:
            logger.warning("%s timed out on %s", agent_name, fd.filename)
        except Exception as exc:
            logger.warning("%s error on %s: %s", agent_name, fd.filename, exc)
        return None

    raw = await asyncio.gather(*[_review_one(fd) for fd in file_diffs])
    results = [r.model_dump() for r in raw if r is not None]

    current = dict(state.get("agent_results") or {})
    current[agent_name] = results
    return _safe_update(state, {"agent_results": current})


async def run_style_agent(state: ReviewState) -> ReviewState:
    return await _run_agent_node(state, "StyleAgent")


async def run_security_agent(state: ReviewState) -> ReviewState:
    return await _run_agent_node(state, "SecurityAgent")


async def run_logic_agent(state: ReviewState) -> ReviewState:
    return await _run_agent_node(state, "LogicAgent")


async def run_performance_agent(state: ReviewState) -> ReviewState:
    return await _run_agent_node(state, "PerformanceAgent")


# ---------------------------------------------------------------------------
# Node: aggregate
# ---------------------------------------------------------------------------

def aggregate(state: ReviewState) -> ReviewState:
    """Call Aggregator and populate state["report"]."""
    if state.get("error"):
        return state
    try:
        all_results: List[AgentResult] = []
        for agent_name, result_dicts in (state.get("agent_results") or {}).items():
            for rd in result_dicts:
                all_results.append(AgentResult(**rd))

        agg = Aggregator(api_key=settings.ANTHROPIC_API_KEY)
        report = agg.aggregate(
            all_results,
            pr_url=state["pr_url"],
            task_id=state.get("task_id"),
            pr_metadata=state.get("pr_metadata") or {},
        )
        return _safe_update(state, {"report": report.model_dump(), "error": None})
    except Exception as exc:
        logger.error("aggregate failed: %s", exc)
        return _safe_update(state, {"error": f"aggregate: {exc}"})


# ---------------------------------------------------------------------------
# Node: save_results
# ---------------------------------------------------------------------------

def save_results(state: ReviewState) -> ReviewState:
    """Persist report to DB and update Redis task status."""
    if state.get("error"):
        return state
    try:
        asyncio.get_event_loop().run_until_complete(_save_results_async(state))
        return _safe_update(state, {"error": None})
    except Exception as exc:
        logger.error("save_results failed: %s", exc)
        return _safe_update(state, {"error": f"save_results: {exc}"})


async def _save_results_async(state: ReviewState) -> None:
    task_id = state["task_id"]
    report_dict = state.get("report") or {}

    from agents.aggregator import AggregatedReport
    report = AggregatedReport(**report_dict)

    async with AsyncSessionLocal() as session:
        for agent_name, result_dicts in (state.get("agent_results") or {}).items():
            for rd in result_dicts:
                ar = AgentResult(**rd)
                avg_conf = (
                    sum(f.confidence for f in ar.findings) / len(ar.findings)
                    if ar.findings else 0.0
                )
                session.add(ReviewResult(
                    task_id=task_id,
                    agent_name=ar.agent_name,
                    findings=ar.model_dump(),
                    confidence=avg_conf,
                ))

        session.add(ReviewReport(
            task_id=task_id,
            final_report=report.model_dump_json(),
            markdown_report=report.markdown_report,
        ))

        task = await session.get(ReviewTask, task_id)
        if task:
            task.status = TaskStatus.COMPLETED

        await session.commit()

    await set_task_status(task_id, TaskStatus.COMPLETED.value)

    # Post markdown report as a PR comment
    GitHubClient().post_review_comment(state["pr_url"], report.markdown_report)


# ---------------------------------------------------------------------------
# Node: error_handler
# ---------------------------------------------------------------------------

def error_handler(state: ReviewState) -> ReviewState:
    """Record failure in DB and Redis."""
    task_id = state.get("task_id")
    error_msg = state.get("error", "unknown error")
    logger.error("[task=%s] Workflow error: %s", task_id, error_msg)
    if task_id is not None:
        try:
            asyncio.get_event_loop().run_until_complete(
                _mark_failed_async(task_id)
            )
        except Exception as exc:
            logger.error("error_handler persistence failed: %s", exc)
    return state


async def _mark_failed_async(task_id: int) -> None:
    await set_task_status(task_id, TaskStatus.FAILED.value)
    async with AsyncSessionLocal() as session:
        task = await session.get(ReviewTask, task_id)
        if task:
            task.status = TaskStatus.FAILED
            await session.commit()


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def _route(node_name: str):
    """Return a conditional edge function that routes to error_handler on error."""
    next_node = {
        "fetch_diff":      "dispatch_agents",
        "dispatch_agents": "run_style",
        "aggregate":       "save_results",
        "save_results":    "__end__",
    }.get(node_name, "__end__")

    def _fn(state: ReviewState) -> str:
        return "error_handler" if state.get("error") else next_node

    _fn.__name__ = f"route_{node_name}"
    return _fn


def build_workflow():
    """Construct and compile the LangGraph StateGraph."""
    from langgraph.graph import END, StateGraph

    graph = StateGraph(ReviewState)

    # Register nodes
    graph.add_node("fetch_diff",          fetch_diff)
    graph.add_node("dispatch_agents",     dispatch_agents)
    graph.add_node("run_style",           run_style_agent)
    graph.add_node("run_security",        run_security_agent)
    graph.add_node("run_logic",           run_logic_agent)
    graph.add_node("run_performance",     run_performance_agent)
    graph.add_node("aggregate",           aggregate)
    graph.add_node("save_results",        save_results)
    graph.add_node("error_handler",       error_handler)

    graph.set_entry_point("fetch_diff")

    # fetch_diff → dispatch_agents (or error_handler on failure)
    graph.add_conditional_edges(
        "fetch_diff",
        lambda s: "error_handler" if s.get("error") else "dispatch_agents",
    )

    # fan-out: dispatch_agents → all four agent nodes
    # (dispatch_agents short-circuits internally on error, so no conditional needed)
    graph.add_edge("dispatch_agents", "run_style")
    graph.add_edge("dispatch_agents", "run_security")
    graph.add_edge("dispatch_agents", "run_logic")
    graph.add_edge("dispatch_agents", "run_performance")

    # fan-in: all agent nodes → aggregate
    graph.add_edge("run_style",       "aggregate")
    graph.add_edge("run_security",    "aggregate")
    graph.add_edge("run_logic",       "aggregate")
    graph.add_edge("run_performance", "aggregate")

    # aggregate → save_results (or error_handler on failure)
    graph.add_conditional_edges(
        "aggregate",
        lambda s: "error_handler" if s.get("error") else "save_results",
    )

    # save_results → END (or error_handler on failure)
    graph.add_conditional_edges(
        "save_results",
        lambda s: "error_handler" if s.get("error") else END,
    )

    graph.add_edge("error_handler", END)

    return graph.compile()


# Module-level compiled workflow instance
workflow = build_workflow()
