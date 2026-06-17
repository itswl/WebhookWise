"""Circuit breaker with Redis-shared state — prevents cascading failures."""

import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import ParamSpec, TypeVar

import httpx
from redis.exceptions import RedisError

from core.logger import get_logger
from core.observability.events import add_span_event, record_signal
from core.observability.metrics import CIRCUIT_BREAKER_STATE
from core.redis_lua import (
    CIRCUIT_BREAKER_CHECK_STATE as _CB_CHECK_STATE_LUA,
)
from core.redis_lua import (
    CIRCUIT_BREAKER_RECORD_FAILURE as _CB_RECORD_FAILURE_LUA,
)
from core.redis_lua import (
    CIRCUIT_BREAKER_RECORD_SUCCESS as _CB_RECORD_SUCCESS_LUA,
)

logger = get_logger("circuit_breaker")


# Upper bound on how long an observed CLOSED state is trusted without re-reading
# Redis. Kept short so a cross-process trip propagates quickly; also capped per
# breaker at its recovery_timeout.
_CLOSED_CACHE_TTL_SECONDS = 3.0


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"


class CircuitBreakerOpenException(RuntimeError):
    """Raised when a circuit breaker rejects a call before executing it."""

    def __init__(self, breaker_name: str) -> None:
        self.breaker_name = breaker_name
        super().__init__(f"CircuitBreaker [{breaker_name}] is open")


class CircuitBreaker:
    """
    Circuit breaker with Redis-shared state.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple[type[BaseException], ...] = (httpx.RequestError, httpx.HTTPStatusError),
        failure_window: int = 60,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions
        self.failure_window = failure_window

        self._prefix = f"circuit_breaker:{name}"
        self._failures_key = f"{self._prefix}:failures"
        self._state_key = f"{self._prefix}:state"
        self._open_until_key = f"{self._prefix}:open_until"

        # Short-lived in-memory cache of an observed CLOSED state, to skip the
        # per-call Redis round-trip on the (overwhelmingly common) healthy path.
        # ONLY CLOSED is ever cached — OPEN always re-reads Redis so recovery and
        # cross-process trips stay responsive. Capped at recovery_timeout so a
        # stale CLOSED can never outlive a recovery window, and invalidated on any
        # locally-recorded failure. Cross-process trips lag by at most this TTL.
        self._closed_cache_ttl = min(_CLOSED_CACHE_TTL_SECONDS, float(recovery_timeout))
        self._closed_cache_until = 0.0

    def _record_state_metric(self, state: CircuitState) -> None:
        for candidate in CircuitState:
            CIRCUIT_BREAKER_STATE.labels(self.name, candidate.value).set(1 if candidate == state else 0)

    async def _check_state_async(self) -> CircuitState:
        from core.redis_health import ensure_redis_available, mark_redis_failure

        # Fast path: a recently-observed CLOSED is trusted for a short window,
        # skipping the Redis round-trip on the healthy path. Never cache OPEN.
        if self._closed_cache_ttl > 0 and time.time() < self._closed_cache_until:
            self._record_state_metric(CircuitState.CLOSED)
            return CircuitState.CLOSED

        if not await ensure_redis_available(f"circuit_breaker:{self.name}:check_state"):
            logger.warning("CircuitBreaker [%s] Redis unavailable; degrading to allow", self.name)
            state = CircuitState.CLOSED
            self._record_state_metric(state)
            return state

        try:
            from core.redis_client import redis_eval_str

            state_str = await redis_eval_str(
                _CB_CHECK_STATE_LUA, 2, self._state_key, self._open_until_key, str(time.time())
            )
            state = CircuitState(state_str) if state_str else CircuitState.CLOSED
        except (RedisError, RuntimeError, TypeError, ValueError) as e:
            mark_redis_failure(f"circuit_breaker:{self.name}:check_state", e)
            logger.warning("CircuitBreaker [%s] Redis state check failed; degrading to allow: %s", self.name, e)
            state = CircuitState.CLOSED
        # Only cache a genuine CLOSED read from Redis; OPEN (and the degraded
        # fallbacks above) are left uncached so they re-check every call.
        if state == CircuitState.CLOSED and self._closed_cache_ttl > 0:
            self._closed_cache_until = time.time() + self._closed_cache_ttl
        self._record_state_metric(state)
        return state

    async def _record_failure(self) -> bool:
        from core.redis_health import ensure_redis_available, mark_redis_failure

        # A local failure moves this breaker toward (or past) the threshold, so
        # drop any cached CLOSED — the next check must re-read Redis.
        self._closed_cache_until = 0.0

        if not await ensure_redis_available(f"circuit_breaker:{self.name}:record_failure"):
            return True

        try:
            from core.redis_client import redis_eval_int

            open_until_ts = str(time.time() + self.recovery_timeout)
            state_expire = int(self.recovery_timeout * 2) + 1
            tripped = await redis_eval_int(
                _CB_RECORD_FAILURE_LUA,
                3,
                self._failures_key,
                self._state_key,
                self._open_until_key,
                str(self.failure_window),
                str(self.failure_threshold),
                open_until_ts,
                str(state_expire),
            )
            return tripped == 1
        except (RedisError, RuntimeError, TypeError, ValueError) as e:
            mark_redis_failure(f"circuit_breaker:{self.name}:record_failure", e)
            logger.warning("CircuitBreaker [%s] Error recording failure to Redis: %s", self.name, e)
            return True

    async def _record_success(self) -> None:
        from core.redis_health import ensure_redis_available, mark_redis_failure

        if not await ensure_redis_available(f"circuit_breaker:{self.name}:record_success"):
            return

        try:
            from core.redis_client import redis_eval_int

            await redis_eval_int(_CB_RECORD_SUCCESS_LUA, 3, self._failures_key, self._state_key, self._open_until_key)
        except (RedisError, RuntimeError, TypeError, ValueError) as e:
            mark_redis_failure(f"circuit_breaker:{self.name}:record_success", e)
            logger.warning("CircuitBreaker [%s] Error recording success to Redis: %s", self.name, e)

    _P = ParamSpec("_P")
    _R = TypeVar("_R")

    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if self.failure_threshold == 0:
            try:
                result = await func(*args, **kwargs)
                return result
            except self.expected_exceptions as e:
                logger.warning("CircuitBreaker [%s] Request error: %s", self.name, e)
                raise

        current_state = await self._check_state_async()
        if current_state == CircuitState.OPEN:
            record_signal("circuit_breaker", "open", {"circuit_breaker.name": self.name})
            add_span_event(
                "circuit_breaker.open",
                {"circuit_breaker.name": self.name, "circuit_breaker.state": current_state.value},
            )
            logger.warning("CircuitBreaker [%s] OPEN — request rejected", self.name)
            raise CircuitBreakerOpenException(self.name)

        try:
            result = await func(*args, **kwargs)
            await self._record_success()
            return result
        except self.expected_exceptions as e:
            tripped = await self._record_failure()
            if tripped:
                self._record_state_metric(CircuitState.OPEN)
                record_signal("circuit_breaker", "open", {"circuit_breaker.name": self.name})
                add_span_event(
                    "circuit_breaker.tripped",
                    {
                        "circuit_breaker.name": self.name,
                        "circuit_breaker.failure_threshold": self.failure_threshold,
                        "circuit_breaker.recovery_timeout": self.recovery_timeout,
                        "error.type": type(e).__name__,
                    },
                )
                logger.error(
                    "CircuitBreaker [%s] Tripped: reached the threshold of %d failures, will recover after %.1fs",
                    self.name,
                    self.failure_threshold,
                    self.recovery_timeout,
                )
            logger.warning("CircuitBreaker [%s] Request error: %s", self.name, e)
            raise
