"""Tests for the Redis hot-path optimizations (perf batch 1).

- EVALSHA with NOSCRIPT -> EVAL fallback
- redis_eval_int_list parsing
- single-round-trip INCR+EXPIRE
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import NoScriptError


class _FakeRedis:
    def __init__(self) -> None:
        self.evalsha_calls = 0
        self.eval_calls = 0
        self.loaded: set[str] = set()

    async def evalsha(self, sha: str, _numkeys: int, *_args: object) -> object:
        self.evalsha_calls += 1
        if sha not in self.loaded:
            raise NoScriptError("NOSCRIPT")
        return [0, 7]

    async def eval(self, _script: str, _numkeys: int, *_args: object) -> object:
        self.eval_calls += 1
        self.loaded.add(__import__("hashlib").sha1(_script.encode()).hexdigest())  # noqa: S324
        return [0, 7]


@pytest.fixture
def _ctx(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    from core import app_context, redis_client

    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    # Avoid metric/otel machinery noise; record_redis_operation still runs but
    # only wraps the awaitable.
    redis_client._SCRIPT_SHA_CACHE.clear()
    _ = app_context  # silence unused import in some refactors
    return fake


@pytest.mark.asyncio
async def test_eval_falls_back_to_eval_on_noscript_then_uses_evalsha(_ctx: _FakeRedis) -> None:
    from core import redis_client

    # First call: evalsha raises NOSCRIPT, falls back to eval (which loads it).
    result = await redis_client.redis_eval_int_list("return {0,7}", 1, "k", 1)
    assert result == [0, 7]
    assert _ctx.evalsha_calls == 1
    assert _ctx.eval_calls == 1

    # Second call: evalsha now succeeds (script loaded), no further eval.
    result2 = await redis_client.redis_eval_int_list("return {0,7}", 1, "k", 1)
    assert result2 == [0, 7]
    assert _ctx.evalsha_calls == 2
    assert _ctx.eval_calls == 1


@pytest.mark.asyncio
async def test_eval_int_list_returns_empty_on_non_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import redis_client

    class _R:
        async def evalsha(self, *_a: object) -> object:
            return "not-a-list"

    monkeypatch.setattr(redis_client, "get_redis", lambda: _R())
    redis_client._SCRIPT_SHA_CACHE.clear()
    assert await redis_client.redis_eval_int_list("x", 0) == []


@pytest.mark.asyncio
async def test_incr_with_expire_single_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import redis_client

    seen: dict[str, Any] = {}

    class _R:
        async def evalsha(self, _sha: str, numkeys: int, *args: object) -> object:
            seen["numkeys"] = numkeys
            seen["args"] = args
            return 5

    monkeypatch.setattr(redis_client, "get_redis", lambda: _R())
    redis_client._SCRIPT_SHA_CACHE.clear()
    # Pre-load the script hash so evalsha doesn't NOSCRIPT.
    val = await redis_client.redis_incr_with_expire("counter", 30)
    assert val == 5
    assert seen["numkeys"] == 1
    assert seen["args"][0] == "counter"
