"""Redis 共享状态熔断器 — 防止级联故障。"""

import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import ParamSpec, TypeVar

import httpx

from core.config import Config

logger = logging.getLogger("webhook_service.circuit_breaker")


class CircuitState(Enum):
    CLOSED = "closed"  # 正常，允许请求通过
    OPEN = "open"  # 熔断，拒绝所有请求
    HALF_OPEN = "half_open"  # 半开，允许试探请求


class CircuitBreakerOpenException(RuntimeError):
    """Raised when a circuit breaker rejects a call before executing it."""

    def __init__(self, breaker_name: str) -> None:
        self.breaker_name = breaker_name
        super().__init__(f"CircuitBreaker [{breaker_name}] is open")


# ====== Lua 脚本：熔断器原子操作 ======

# 记录失败并判断是否需要熔断
# KEYS: [failures_key, state_key, open_until_key]
# ARGV: [failure_window, threshold, open_until_ts, state_expire]
_CB_RECORD_FAILURE_LUA = """
local failures = redis.call("incr", KEYS[1])
if failures == 1 then
    redis.call("expire", KEYS[1], tonumber(ARGV[1]))
end
if failures >= tonumber(ARGV[2]) then
    redis.call("set", KEYS[2], "open")
    redis.call("set", KEYS[3], ARGV[3])
    redis.call("expire", KEYS[2], tonumber(ARGV[4]))
    redis.call("expire", KEYS[3], tonumber(ARGV[4]))
    return 1
end
return 0
"""

# 记录成功：仅当 state 为 half_open 时重置为 closed
# KEYS: [failures_key, state_key, open_until_key]
_CB_RECORD_SUCCESS_LUA = """
local state = redis.call("get", KEYS[2])
if state == "half_open" or state == "open" then
    redis.call("del", KEYS[1])
    redis.call("set", KEYS[2], "closed")
    redis.call("del", KEYS[3])
end
return 0
"""

# 检查状态：如果 open 且超时则原子转为 half_open
# KEYS: [state_key, open_until_key]
# ARGV: [current_timestamp]
_CB_CHECK_STATE_LUA = """
local state = redis.call("get", KEYS[1])
if not state or state == false then
    return "closed"
end
if state == "open" then
    local open_until = redis.call("get", KEYS[2])
    if open_until and tonumber(ARGV[1]) >= tonumber(open_until) then
        redis.call("set", KEYS[1], "half_open")
        return "half_open"
    end
end
return state
"""


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

    async def _check_state_async(self) -> CircuitState:
        try:
            from core.redis_client import redis_eval_str

            state_str = await redis_eval_str(
                _CB_CHECK_STATE_LUA, 2, self._state_key, self._open_until_key, str(time.time())
            )
            return CircuitState(state_str) if state_str else CircuitState.CLOSED
        except Exception as e:
            logger.warning("CircuitBreaker [%s] Redis 检查状态失败: %s", self.name, e)
            return CircuitState.CLOSED

    async def _record_failure(self) -> bool:
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
            return bool(tripped)
        except Exception as e:
            logger.warning("CircuitBreaker [%s] Redis 记录失败异常: %s", self.name, e)
            return False

    async def _record_success(self) -> None:
        try:
            from core.redis_client import redis_eval_int

            await redis_eval_int(_CB_RECORD_SUCCESS_LUA, 3, self._failures_key, self._state_key, self._open_until_key)
        except Exception as e:
            logger.warning("CircuitBreaker [%s] Redis 记录成功异常: %s", self.name, e)

    _P = ParamSpec("_P")
    _R = TypeVar("_R")

    async def call_async(self, func: Callable[_P, Awaitable[_R]], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if self.failure_threshold == 0:
            try:
                return await func(*args, **kwargs)
            except self.expected_exceptions as e:
                logger.warning("CircuitBreaker [%s] 请求异常: %s", self.name, e)
                raise

        current_state = await self._check_state_async()
        if current_state == CircuitState.OPEN:
            logger.warning("CircuitBreaker [%s] OPEN — 请求被拒绝", self.name)
            raise CircuitBreakerOpenException(self.name)

        try:
            result = await func(*args, **kwargs)
            await self._record_success()
            return result
        except self.expected_exceptions as e:
            tripped = await self._record_failure()
            if tripped:
                logger.error(
                    "CircuitBreaker [%s] 触发熔断: 达到阈值 %d 次, " "将在 %.1fs 后恢复",
                    self.name,
                    self.failure_threshold,
                    self.recovery_timeout,
                )
            logger.warning("CircuitBreaker [%s] 请求异常: %s", self.name, e)
            raise


# 预置熔断器实例
feishu_cb = CircuitBreaker(
    name="feishu",
    failure_threshold=Config.circuit_breaker.CIRCUIT_BREAKER_FEISHU_THRESHOLD,
    recovery_timeout=Config.circuit_breaker.CIRCUIT_BREAKER_FEISHU_TIMEOUT,
)
openclaw_cb = CircuitBreaker(
    name="openclaw",
    failure_threshold=Config.circuit_breaker.CIRCUIT_BREAKER_OPENCLAW_THRESHOLD,
    recovery_timeout=Config.circuit_breaker.CIRCUIT_BREAKER_OPENCLAW_TIMEOUT,
)
forward_cb = CircuitBreaker(
    name="forward",
    failure_threshold=Config.circuit_breaker.CIRCUIT_BREAKER_FORWARD_THRESHOLD,
    recovery_timeout=Config.circuit_breaker.CIRCUIT_BREAKER_FORWARD_TIMEOUT,
)
