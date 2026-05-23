"""Redis 共享状态熔断器 — 防止级联故障。"""

import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import ParamSpec, TypeVar

import httpx

from core.logger import get_logger
from core.observability.events import add_span_event
from core.observability.metrics import (
    CIRCUIT_BREAKER_REQUESTS_TOTAL,
    CIRCUIT_BREAKER_STATE,
    CIRCUIT_BREAKER_TRANSITIONS_TOTAL,
)
from core.observability.signals import record_signal
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


class CircuitState(Enum):
    CLOSED = "closed"  # 正常，允许请求通过
    OPEN = "open"  # 熔断，拒绝所有请求
    HALF_OPEN = "half_open"  # 半开，允许试探请求


class CircuitBreakerOpenException(RuntimeError):
    """Raised when a circuit breaker rejects a call before executing it."""

    def __init__(self, breaker_name: str) -> None:
        self.breaker_name = breaker_name
        super().__init__(f"CircuitBreaker [{breaker_name}] is open")


class CircuitBreaker:
    """
    Redis 共享状态熔断器。
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple[type[BaseException], ...] = (httpx.RequestError,),
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

    def _record_state_metric(self, state: CircuitState) -> None:
        for candidate in CircuitState:
            CIRCUIT_BREAKER_STATE.labels(self.name, candidate.value).set(1 if candidate == state else 0)

    async def _check_state_async(self) -> CircuitState:
        from core.redis_health import ensure_redis_available, mark_redis_failure

        if not await ensure_redis_available(f"circuit_breaker:{self.name}:check_state"):
            logger.warning("CircuitBreaker [%s] Redis 不可用，降级放行", self.name)
            state = CircuitState.CLOSED
            self._record_state_metric(state)
            return state

        try:
            from core.redis_client import redis_eval_str

            state_str = await redis_eval_str(
                _CB_CHECK_STATE_LUA, 2, self._state_key, self._open_until_key, str(time.time())
            )
            state = CircuitState(state_str) if state_str else CircuitState.CLOSED
        except Exception as e:
            mark_redis_failure(f"circuit_breaker:{self.name}:check_state", e)
            logger.warning("CircuitBreaker [%s] Redis 检查状态失败，降级放行: %s", self.name, e)
            state = CircuitState.CLOSED
        self._record_state_metric(state)
        return state

    async def _record_failure(self) -> bool:
        from core.redis_health import ensure_redis_available, mark_redis_failure

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
        except Exception as e:
            mark_redis_failure(f"circuit_breaker:{self.name}:record_failure", e)
            logger.warning("CircuitBreaker [%s] Redis 记录失败异常: %s", self.name, e)
            return True

    async def _record_success(self) -> None:
        from core.redis_health import ensure_redis_available, mark_redis_failure

        if not await ensure_redis_available(f"circuit_breaker:{self.name}:record_success"):
            return

        try:
            from core.redis_client import redis_eval_int

            await redis_eval_int(_CB_RECORD_SUCCESS_LUA, 3, self._failures_key, self._state_key, self._open_until_key)
        except Exception as e:
            mark_redis_failure(f"circuit_breaker:{self.name}:record_success", e)
            logger.warning("CircuitBreaker [%s] Redis 记录成功异常: %s", self.name, e)

    _P = ParamSpec("_P")
    _R = TypeVar("_R")

    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if self.failure_threshold == 0:
            try:
                result = await func(*args, **kwargs)
                CIRCUIT_BREAKER_REQUESTS_TOTAL.labels(self.name, "disabled_success").inc()
                return result
            except self.expected_exceptions as e:
                CIRCUIT_BREAKER_REQUESTS_TOTAL.labels(self.name, "disabled_failure").inc()
                logger.warning("CircuitBreaker [%s] 请求异常: %s", self.name, e)
                raise

        current_state = await self._check_state_async()
        if current_state == CircuitState.OPEN:
            CIRCUIT_BREAKER_REQUESTS_TOTAL.labels(self.name, "rejected").inc()
            record_signal("circuit_breaker", "open", {"circuit_breaker.name": self.name})
            add_span_event(
                "circuit_breaker.open",
                {"circuit_breaker.name": self.name, "circuit_breaker.state": current_state.value},
            )
            logger.warning("CircuitBreaker [%s] OPEN — 请求被拒绝", self.name)
            raise CircuitBreakerOpenException(self.name)

        try:
            result = await func(*args, **kwargs)
            CIRCUIT_BREAKER_REQUESTS_TOTAL.labels(self.name, "success").inc()
            await self._record_success()
            if current_state == CircuitState.HALF_OPEN:
                CIRCUIT_BREAKER_TRANSITIONS_TOTAL.labels(self.name, CircuitState.CLOSED.value).inc()
                self._record_state_metric(CircuitState.CLOSED)
                record_signal("circuit_breaker", "closed", {"circuit_breaker.name": self.name})
            return result
        except self.expected_exceptions as e:
            CIRCUIT_BREAKER_REQUESTS_TOTAL.labels(self.name, "failure").inc()
            tripped = await self._record_failure()
            if tripped:
                CIRCUIT_BREAKER_TRANSITIONS_TOTAL.labels(self.name, CircuitState.OPEN.value).inc()
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
                    "CircuitBreaker [%s] 触发熔断: 达到阈值 %d 次, 将在 %.1fs 后恢复",
                    self.name,
                    self.failure_threshold,
                    self.recovery_timeout,
                )
            logger.warning("CircuitBreaker [%s] 请求异常: %s", self.name, e)
            raise
