"""Redis-backed cache helpers for task status, agent results, and dedup."""

import hashlib
import json
import logging
from typing import Any

from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError

from config import settings

logger = logging.getLogger(__name__)

# Key templates
_STATUS_KEY = "codereview:task:{task_id}:status"
_AGENT_KEY = "codereview:task:{task_id}:agent:{agent_slot}"
_AGENT_INDEX_KEY = "codereview:task:{task_id}:agents"
_DEDUP_KEY = "codereview:dedup:{pr_url_hash}:{commit_sha}"

# Default TTL: 24 hours
_DEFAULT_TTL = 86_400


def _get_client() -> Redis:
    """Return a lazily-initialised async Redis client."""
    return from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)


# ---------------------------------------------------------------------------
# Task status
# ---------------------------------------------------------------------------

async def set_task_status(task_id: int | str, status: str, ttl: int = _DEFAULT_TTL) -> None:
    """
    Persist *status* for *task_id* in Redis.

    Args:
        task_id: Unique task identifier.
        status:  One of pending / running / completed / partial / uncovered / failed.
        ttl:     Time-to-live in seconds (default 24 h).
    """
    try:
        async with _get_client() as redis:
            key = _STATUS_KEY.format(task_id=task_id)
            await redis.set(key, status, ex=ttl)
    except RedisError as exc:
        logger.warning("Redis unavailable while setting task status for %s: %s", task_id, exc)


async def get_task_status(task_id: int | str) -> str | None:
    """
    Retrieve the cached status for *task_id*.

    Returns:
        Status string, or ``None`` if the key does not exist / has expired.
    """
    try:
        async with _get_client() as redis:
            key = _STATUS_KEY.format(task_id=task_id)
            return await redis.get(key)
    except RedisError as exc:
        logger.warning("Redis unavailable while reading task status for %s: %s", task_id, exc)
        return None


# ---------------------------------------------------------------------------
# Agent results
# ---------------------------------------------------------------------------

async def set_agent_result(
    task_id: int | str,
    agent_name: str,
    result: dict[str, Any],
    ttl: int = _DEFAULT_TTL,
    *,
    filename: str | None = None,
) -> None:
    """
    Store a single agent's result JSON and register *agent_name* in the
    per-task agent index so it can be retrieved with
    :func:`get_all_agent_results`.

    Args:
        task_id:    Unique task identifier.
        agent_name: Name of the agent (e.g. ``"security_agent"``).
        result:     Serialisable dict produced by the agent.
        filename:   Changed file associated with this attempt.  It is part of
                    the Redis key so one agent cannot overwrite another file.
        ttl:        Time-to-live in seconds (default 24 h).
    """
    try:
        async with _get_client() as redis:
            agent_slot = _agent_slot(agent_name, filename)
            # Store the result payload
            data_key = _AGENT_KEY.format(task_id=task_id, agent_slot=agent_slot)
            payload = dict(result)
            payload.setdefault("agent_name", agent_name)
            if filename is not None:
                payload.setdefault("filename", filename)
            await redis.set(data_key, json.dumps(payload), ex=ttl)

            # Register the complete slot in the set index so every per-file
            # result can be enumerated later.
            index_key = _AGENT_INDEX_KEY.format(task_id=task_id)
            await redis.sadd(index_key, agent_slot)
            await redis.expire(index_key, ttl)
    except RedisError as exc:
        logger.warning(
            "Redis unavailable while caching agent result for task %s (%s): %s",
            task_id,
            agent_name,
            exc,
        )


# ---------------------------------------------------------------------------
# PR dedup cache
# ---------------------------------------------------------------------------

def _pr_url_hash(pr_url: str) -> str:
    return hashlib.sha256(pr_url.encode()).hexdigest()[:16]


def _agent_slot(agent_name: str, filename: str | None = None) -> str:
    """Return a stable Redis-safe slot for one agent/file attempt."""
    if filename is None:
        return agent_name
    file_hash = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]
    return f"{agent_name}:{file_hash}"


async def get_dedup_task_id(pr_url: str, commit_sha: str) -> int | None:
    """
    Return the task_id of a previously completed review for the same
    PR + commit SHA, or None if no cached result exists. Only complete
    reviews are written to this cache by the orchestrator.
    """
    try:
        async with _get_client() as redis:
            key = _DEDUP_KEY.format(
                pr_url_hash=_pr_url_hash(pr_url),
                commit_sha=commit_sha,
            )
            raw = await redis.get(key)
            return int(raw) if raw is not None else None
    except (RedisError, TypeError, ValueError) as exc:
        logger.warning("Redis unavailable while reading dedup cache for %s@%s: %s", pr_url, commit_sha, exc)
        return None


async def set_dedup_task_id(
    pr_url: str,
    commit_sha: str,
    task_id: int,
    ttl: int | None = None,
) -> None:
    """
    Record that *task_id* is the canonical review for this PR + commit SHA.
    """
    try:
        async with _get_client() as redis:
            key = _DEDUP_KEY.format(
                pr_url_hash=_pr_url_hash(pr_url),
                commit_sha=commit_sha,
            )
            await redis.set(key, str(task_id), ex=ttl or settings.DEDUP_CACHE_TTL)
    except RedisError as exc:
        logger.warning("Redis unavailable while writing dedup cache for %s@%s: %s", pr_url, commit_sha, exc)


async def get_all_agent_results(task_id: int | str) -> dict[str, Any]:
    """
    Retrieve all agent results that have been stored for *task_id*.

    Returns:
        A mapping of ``agent_name -> result_dict``.  Empty dict when no
        results are cached yet.
    """
    try:
        async with _get_client() as redis:
            index_key = _AGENT_INDEX_KEY.format(task_id=task_id)
            agent_names: set[str] = await redis.smembers(index_key)

            if not agent_names:
                return {}

            results: dict[str, Any] = {}
            for agent_slot in agent_names:
                data_key = _AGENT_KEY.format(task_id=task_id, agent_slot=agent_slot)
                raw = await redis.get(data_key)
                if raw is not None:
                    try:
                        results[agent_slot] = json.loads(raw)
                    except (TypeError, ValueError):
                        logger.warning(
                            "Ignoring malformed cached agent result for task %s (%s)",
                            task_id,
                            agent_slot,
                        )

            return results
    except RedisError as exc:
        logger.warning("Redis unavailable while reading cached agent results for task %s: %s", task_id, exc)
        return {}
