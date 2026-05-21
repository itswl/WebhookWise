from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from shutil import which

import pytest
import redis.asyncio as redis

pytestmark = pytest.mark.real_redis


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
async def real_redis(tmp_path: Path) -> AsyncGenerator[redis.Redis, None]:
    redis_server = which("redis-server")
    if redis_server is None:
        pytest.skip("redis-server is not installed")

    port = _free_port()
    proc = subprocess.Popen(
        [
            redis_server,
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    try:
        deadline = time.monotonic() + 3
        while True:
            try:
                await client.ping()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        await client.flushdb()
        yield client
    finally:
        await client.aclose()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


async def test_alert_concurrency_queue_slot_lua_does_not_count_suppressed_requests(real_redis: redis.Redis) -> None:
    from core.redis_lua import ALERT_RELEASE_QUEUE_SLOT, ALERT_RESERVE_QUEUE_SLOT

    key = "test:queue:hot-alert"

    assert await real_redis.eval(ALERT_RESERVE_QUEUE_SLOT, 1, key, 30, 2) == 1
    assert await real_redis.eval(ALERT_RESERVE_QUEUE_SLOT, 1, key, 30, 2) == 2
    assert await real_redis.eval(ALERT_RESERVE_QUEUE_SLOT, 1, key, 30, 2) == -2
    assert await real_redis.get(key) == "2"

    assert await real_redis.eval(ALERT_RELEASE_QUEUE_SLOT, 1, key, 30) == 1
    assert await real_redis.eval(ALERT_RELEASE_QUEUE_SLOT, 1, key, 30) == 0
    assert await real_redis.exists(key) == 0


async def test_alert_concurrency_lock_lua_respects_owner_tokens(real_redis: redis.Redis) -> None:
    from core.redis_lua import ALERT_REFRESH_LOCK_IF_OWNER, ALERT_RELEASE_LOCK_IF_OWNER

    key = "test:lock:alert"
    await real_redis.set(key, "owner-a")

    assert await real_redis.eval(ALERT_RELEASE_LOCK_IF_OWNER, 1, key, "owner-b") == 0
    assert await real_redis.get(key) == "owner-a"
    assert await real_redis.eval(ALERT_REFRESH_LOCK_IF_OWNER, 1, key, "owner-b", 30) == 0

    assert await real_redis.eval(ALERT_REFRESH_LOCK_IF_OWNER, 1, key, "owner-a", 30) == 1
    assert await real_redis.ttl(key) > 0
    assert await real_redis.eval(ALERT_RELEASE_LOCK_IF_OWNER, 1, key, "owner-a") == 1
    assert await real_redis.exists(key) == 0


async def test_sliding_window_rate_limit_lua_rejects_after_limit(real_redis: redis.Redis) -> None:
    from core.redis_lua import SLIDING_WINDOW_RATE_LIMIT

    prefix = "test:rate-limit"
    now = 1_700_000_000.0

    assert await real_redis.eval(SLIDING_WINDOW_RATE_LIMIT, 1, prefix, 60, 2, now) == 1
    assert await real_redis.eval(SLIDING_WINDOW_RATE_LIMIT, 1, prefix, 60, 2, now + 1) == 0
    assert await real_redis.eval(SLIDING_WINDOW_RATE_LIMIT, 1, prefix, 60, 2, now + 2) == -1


async def test_circuit_breaker_lua_transitions_open_half_open_closed(real_redis: redis.Redis) -> None:
    from core.redis_lua import (
        CIRCUIT_BREAKER_CHECK_STATE,
        CIRCUIT_BREAKER_RECORD_FAILURE,
        CIRCUIT_BREAKER_RECORD_SUCCESS,
    )

    failures_key = "test:cb:failures"
    state_key = "test:cb:state"
    open_until_key = "test:cb:open_until"

    assert (
        await real_redis.eval(
            CIRCUIT_BREAKER_RECORD_FAILURE,
            3,
            failures_key,
            state_key,
            open_until_key,
            60,
            2,
            "2000",
            120,
        )
        == 0
    )
    assert (
        await real_redis.eval(
            CIRCUIT_BREAKER_RECORD_FAILURE,
            3,
            failures_key,
            state_key,
            open_until_key,
            60,
            2,
            "2000",
            120,
        )
        == 1
    )
    assert await real_redis.eval(CIRCUIT_BREAKER_CHECK_STATE, 2, state_key, open_until_key, "1000") == "open"
    assert await real_redis.eval(CIRCUIT_BREAKER_CHECK_STATE, 2, state_key, open_until_key, "2000") == "half_open"

    assert await real_redis.eval(CIRCUIT_BREAKER_RECORD_SUCCESS, 3, failures_key, state_key, open_until_key) == 0
    assert await real_redis.eval(CIRCUIT_BREAKER_CHECK_STATE, 2, state_key, open_until_key, "2001") == "closed"
    assert await real_redis.exists(failures_key) == 0
