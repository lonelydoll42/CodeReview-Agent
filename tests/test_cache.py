"""Tests for Redis cache degradation behavior."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from redis.exceptions import RedisError

from storage.cache import get_all_agent_results, get_dedup_task_id, set_agent_result, set_task_status


@pytest.mark.asyncio
async def test_get_dedup_task_id_returns_none_when_redis_unavailable():
    """Dedup lookup should degrade gracefully when Redis is down."""
    with patch("storage.cache._get_client", side_effect=RedisError("down")):
        cached_id = await get_dedup_task_id(
            "https://github.com/owner/repo/pull/1",
            "abc123",
        )

    assert cached_id is None


@pytest.mark.asyncio
async def test_get_dedup_task_id_ignores_corrupt_cached_task_id():
    """A manually corrupted dedup entry should behave like a cache miss."""
    class FakeRedis:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _key):
            return "not-a-task-id"

    with patch("storage.cache._get_client", return_value=FakeRedis()):
        cached_id = await get_dedup_task_id(
            "https://github.com/owner/repo/pull/1",
            "abc123",
        )

    assert cached_id is None


@pytest.mark.asyncio
async def test_set_task_status_does_not_raise_when_redis_unavailable():
    """Task status writes should be best-effort, not fatal."""
    with patch("storage.cache._get_client", side_effect=RedisError("down")):
        await set_task_status(1, "pending")


@pytest.mark.asyncio
async def test_agent_results_are_keyed_by_file_for_same_agent():
    """Per-file attempts from one agent must not overwrite one another."""
    class FakeRedis:
        def __init__(self):
            self.values: dict[str, str] = {}
            self.members: set[str] = set()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def set(self, key, value, ex=None):
            self.values[key] = value

        async def sadd(self, _key, member):
            self.members.add(member)

        async def expire(self, *_args):
            return True

        async def smembers(self, _key):
            return set(self.members)

        async def get(self, key):
            return self.values.get(key)

    redis = FakeRedis()
    with patch("storage.cache._get_client", return_value=redis):
        await set_agent_result(1, "LogicAgent", {"status": "completed"}, filename="a.py")
        await set_agent_result(1, "LogicAgent", {"status": "completed"}, filename="b.py")
        results = await get_all_agent_results(1)

    assert len(results) == 2
    assert {payload["filename"] for payload in results.values()} == {"a.py", "b.py"}
