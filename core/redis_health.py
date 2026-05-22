from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, cast

from core.logger import get_logger

if TYPE_CHECKING:
    from core.redis_lifecycle import RedisClient

logger = get_logger("redis_health")

_RECOVERY_PROBE_INTERVAL_SECONDS = 5.0


class RedisHealthState(Enum):
    HEALTHY = "healthy"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RedisHealthSnapshot:
    state: RedisHealthState
    consecutive_failures: int
    last_success_at: float | None
    last_failure_at: float | None
    last_error: str
    last_operation: str


_state = RedisHealthState.HEALTHY
_consecutive_failures = 0
_last_success_at: float | None = None
_last_failure_at: float | None = None
_last_error = ""
_last_operation = ""
_last_probe_monotonic = 0.0
_probe_lock = asyncio.Lock()


def _record_state_metric() -> None:
    try:
        from core.observability.metrics import REDIS_HEALTH_STATE

        for candidate in RedisHealthState:
            REDIS_HEALTH_STATE.labels(candidate.value).set(1 if candidate == _state else 0)
    except Exception:
        logger.debug("[RedisHealth] failed to record health metric", exc_info=True)


def get_redis_health_snapshot() -> RedisHealthSnapshot:
    return RedisHealthSnapshot(
        state=_state,
        consecutive_failures=_consecutive_failures,
        last_success_at=_last_success_at,
        last_failure_at=_last_failure_at,
        last_error=_last_error,
        last_operation=_last_operation,
    )


def redis_is_available() -> bool:
    return _state == RedisHealthState.HEALTHY


def mark_redis_success(operation: str) -> None:
    global _state, _consecutive_failures, _last_success_at, _last_error, _last_operation
    _state = RedisHealthState.HEALTHY
    _consecutive_failures = 0
    _last_success_at = time.time()
    _last_error = ""
    _last_operation = operation
    _record_state_metric()


def mark_redis_failure(operation: str, error: BaseException) -> None:
    global _state, _consecutive_failures, _last_failure_at, _last_error, _last_operation, _last_probe_monotonic
    _state = RedisHealthState.UNAVAILABLE
    _consecutive_failures += 1
    _last_failure_at = time.time()
    _last_error = f"{type(error).__name__}: {error}"
    _last_operation = operation
    _last_probe_monotonic = time.monotonic()
    _record_state_metric()


async def _ping_redis(client: RedisClient) -> bool:
    maybe_ping = client.ping()
    raw = await cast(Awaitable[object], maybe_ping) if inspect.isawaitable(maybe_ping) else maybe_ping
    return bool(raw)


async def ensure_redis_available(operation: str, *, probe_interval: float = _RECOVERY_PROBE_INTERVAL_SECONDS) -> bool:
    """Return whether Redis should be used for a control-plane operation.

    Healthy state is optimistic and avoids an extra ping. Once any Redis helper
    reports an error, callers pause Redis-dependent control paths until a
    throttled health probe succeeds.
    """

    global _last_probe_monotonic
    if redis_is_available():
        return True

    now = time.monotonic()
    if now - _last_probe_monotonic < probe_interval:
        return False

    async with _probe_lock:
        now = time.monotonic()
        if redis_is_available():
            return True
        if now - _last_probe_monotonic < probe_interval:
            return False
        _last_probe_monotonic = now

        try:
            from core import redis_lifecycle

            if await _ping_redis(redis_lifecycle.get_redis()):
                mark_redis_success(f"{operation}:health_probe")
                logger.info("[RedisHealth] Redis health probe recovered operation=%s", operation)
                return True
            raise RuntimeError("Redis health probe returned false")
        except Exception as e:
            mark_redis_failure(f"{operation}:health_probe", e)
            logger.warning("[RedisHealth] Redis health probe failed operation=%s error=%s", operation, e)
            return False


def reset_redis_health() -> None:
    global _state, _consecutive_failures, _last_success_at, _last_failure_at, _last_error, _last_operation
    global _last_probe_monotonic
    _state = RedisHealthState.HEALTHY
    _consecutive_failures = 0
    _last_success_at = None
    _last_failure_at = None
    _last_error = ""
    _last_operation = ""
    _last_probe_monotonic = 0.0
    _record_state_metric()
