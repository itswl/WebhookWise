import hashlib
import hmac
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import httpx
import orjson

from core.config import Config
from core.logger import logger


def mask_url(url: str) -> str:
    """安全脱敏 URL，移除用户名和密码，仅保留 scheme + host + port + path。"""
    try:
        parsed = urlparse(url)
        if parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://***@{parsed.hostname}{port}{parsed.path}"
        return "***"
    except Exception:
        return "***"


WebhookData = dict[str, Any]


class CircuitState(Enum):
    CLOSED = "closed"  # 正常，允许请求通过
    OPEN = "open"  # 熔断，拒绝所有请求
    HALF_OPEN = "half_open"  # 半开，允许试探请求


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
    Redis 共享状态熔断器，防止级联故障。

    状态存储在 Redis 中，多 Worker/Pod 共享熔断状态：
    - CLOSED（正常）：请求通过，失败计数；达到阈值后转为 OPEN
    - OPEN（熔断）：请求直接拒绝（返回 None），超时后转为 HALF_OPEN
    - HALF_OPEN（半开）：允许一个试探请求；成功则回 CLOSED，失败则回 OPEN

    所有状态转换通过 Lua 脚本保证原子性。
    Redis 不可用时降级为默认允许请求（fail-open）。
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple = (httpx.RequestError,),
        failure_window: int = 60,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions
        self.failure_window = failure_window  # 失败计数的滑动窗口（秒）

        # Redis key 前缀
        self._prefix = f"circuit_breaker:{name}"
        self._failures_key = f"{self._prefix}:failures"
        self._state_key = f"{self._prefix}:state"
        self._open_until_key = f"{self._prefix}:open_until"

    def _get_redis(self):
        """获取 Redis 客户端，失败返回 None（降级用）"""
        try:
            import core.redis_client

            return core.redis_client.get_redis()
        except Exception as e:
            logger.error(f"CircuitBreaker [{self.name}] 获取 Redis 失败，降级放行: {e}")
            return None

    @property
    def state(self) -> CircuitState:
        """读取当前熔断器状态 — 仅供监控指标拉取和 Debug 日志使用。

        注意：此属性为同步方法，直接返回 CLOSED 作为默认值。
        实际状态存储在 Redis 中，精确状态请使用 _check_state_async()。
        """
        return CircuitState.CLOSED

    async def _check_state_async(self) -> CircuitState:
        """通过 Redis Lua 脚本原子检查并转换状态。"""
        r = self._get_redis()
        if r is None:
            return CircuitState.CLOSED
        try:
            state_str = await r.eval(
                _CB_CHECK_STATE_LUA,
                2,
                self._state_key,
                self._open_until_key,
                str(time.time()),
            )
            return CircuitState(state_str) if state_str else CircuitState.CLOSED
        except Exception as e:
            logger.warning(f"CircuitBreaker [{self.name}] Redis 检查状态失败，降级放行: {e}")
            return CircuitState.CLOSED

    async def _record_failure(self) -> bool:
        """记录一次失败，返回 True 表示触发了熔断。"""
        r = self._get_redis()
        if r is None:
            return False
        try:
            open_until_ts = str(time.time() + self.recovery_timeout)
            # state_expire: recovery_timeout 的 2 倍，确保 key 不会提前过期
            state_expire = int(self.recovery_timeout * 2) + 1
            tripped = await r.eval(
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
            logger.warning(f"CircuitBreaker [{self.name}] Redis 记录失败异常，降级忽略: {e}")
            return False

    async def _record_success(self):
        """记录一次成功，如果是 half_open 状态则重置为 closed。"""
        r = self._get_redis()
        if r is None:
            return
        try:
            await r.eval(
                _CB_RECORD_SUCCESS_LUA,
                3,
                self._failures_key,
                self._state_key,
                self._open_until_key,
            )
        except Exception as e:
            logger.warning(f"CircuitBreaker [{self.name}] Redis 记录成功异常，降级忽略: {e}")

    async def call_async(self, func: Callable, *args, **kwargs):
        """异步执行函数，失败时触发熔断（Redis 共享状态 + Lua 原子操作）。"""
        if self.failure_threshold == 0:
            try:
                return await func(*args, **kwargs)
            except self.expected_exceptions as e:
                logger.warning(f"CircuitBreaker [{self.name}] 请求异常（已禁用）: {e}")
                return None

        current_state = await self._check_state_async()
        if current_state == CircuitState.OPEN:
            logger.warning(f"CircuitBreaker [{self.name}] OPEN — 请求被拒绝")
            return None

        try:
            result = await func(*args, **kwargs)
            await self._record_success()
            return result
        except self.expected_exceptions as e:
            tripped = await self._record_failure()
            if tripped:
                logger.error(
                    f"CircuitBreaker [{self.name}] 触发熔断: "
                    f"达到阈值 {self.failure_threshold} 次, "
                    f"将在 {self.recovery_timeout}s 后恢复"
                )
            logger.warning(f"CircuitBreaker [{self.name}] 请求异常: {e}")
            return None


# 预置熔断器实例（通过 Config）

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


HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]


MAX_SAVE_RETRIES = Config.retry.SAVE_MAX_RETRIES
RETRY_DELAY_SECONDS = Config.retry.SAVE_RETRY_DELAY_SECONDS


def verify_signature(payload: bytes, signature: str, secret: str | None = None) -> bool:
    """验证 webhook 签名"""
    if secret is None:
        secret = Config.security.WEBHOOK_SECRET

    if not secret:
        return False

    expected_signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    result = hmac.compare_digest(expected_signature, signature)
    if not result:
        logger.warning("[Auth] 签名比对不匹配")
    else:
        logger.debug("[Auth] 签名验证通过")
    return result


# ====== 告警哈希字段配置 ======
# Prometheus Alertmanager 格式的字段提取配置
PROMETHEUS_ROOT_FIELDS = ["alertingRuleName"]
PROMETHEUS_LABEL_FIELDS = [
    "alertname",
    "internal_label_alert_level",
    "host",
    "instance",
    "pod",
    "namespace",
    "service",
    "path",
    "method",
]
PROMETHEUS_ALERT_FIELDS = ["fingerprint"]

# 华为云/通用告警格式的字段提取配置
GENERIC_FIELDS = [
    "Type",
    "RuleName",
    "event",
    "event_type",
    "MetricName",
    "Level",
    "alert_id",
    "alert_name",
    "resource_id",
    "service",
]


def _extract_fields(data: dict[str, Any], fields: list[str], lower_keys: bool = True) -> dict[str, Any]:
    """从字典中提取指定字段。"""
    extracted = {}
    for field in fields:
        if field in data:
            key = field.lower() if lower_keys else field
            extracted[key] = data[field]
    return extracted


def _extract_prometheus_fields(data: dict[str, Any]) -> dict[str, Any]:
    """提取 Prometheus Alertmanager 格式的关键字段。"""
    key_fields = _extract_fields(data, PROMETHEUS_ROOT_FIELDS)

    alerts = data.get("alerts", [])
    first_alert = alerts[0] if alerts and isinstance(alerts[0], dict) else None
    if not first_alert:
        return key_fields

    labels = first_alert.get("labels", {})
    if isinstance(labels, dict):
        key_fields.update(_extract_fields(labels, PROMETHEUS_LABEL_FIELDS, lower_keys=False))

    key_fields.update(_extract_fields(first_alert, PROMETHEUS_ALERT_FIELDS, lower_keys=False))
    return key_fields


def _extract_generic_fields(data: dict[str, Any]) -> dict[str, Any]:
    """提取华为云/通用告警格式的关键字段。"""
    key_fields = _extract_fields(data, GENERIC_FIELDS)

    resources = data.get("Resources", [])
    first_resource = (
        resources[0] if isinstance(resources, list) and resources and isinstance(resources[0], dict) else None
    )
    if not first_resource:
        return key_fields

    resource_id = first_resource.get("InstanceId") or first_resource.get("Id") or first_resource.get("id")
    if resource_id:
        key_fields["resource_id"] = resource_id

    dimensions = first_resource.get("Dimensions", [])
    if not isinstance(dimensions, list):
        return key_fields

    important_dims = {"Node", "ResourceID", "Instance", "InstanceId", "Host", "Pod", "Container"}
    for dim in dimensions:
        if not isinstance(dim, dict):
            continue
        dim_name = dim.get("Name", "")
        dim_value = dim.get("Value")
        if dim_name in important_dims and dim_value:
            key_fields[f"dim_{dim_name.lower()}"] = dim_value

    return key_fields


def generate_alert_hash(data: dict[str, Any], source: str) -> str:
    """
    生成告警的唯一哈希值，用于识别重复告警

    Args:
        data: webhook 数据
        source: 数据来源

    Returns:
        str: SHA256 哈希值
    """
    key_fields = {"source": source}

    if isinstance(data, dict):
        is_prometheus = "alerts" in data and isinstance(data.get("alerts"), list) and len(data["alerts"]) > 0

        if is_prometheus:
            key_fields.update(_extract_prometheus_fields(data))
        else:
            key_fields.update(_extract_generic_fields(data))

    key_string = orjson.dumps(key_fields, option=orjson.OPT_SORT_KEYS).decode()
    hash_value = hashlib.sha256(key_string.encode("utf-8")).hexdigest()

    logger.debug("[Hash] 生成告警哈希: hash=%s..., input_keys=%s", hash_value[:16], list(key_fields.keys()))
    return hash_value


@asynccontextmanager
async def processing_lock(alert_hash: str) -> AsyncGenerator["ProcessingLockResult", None]:
    """获取基于 alert_hash 的分布式处理锁。

    保护范围：MQ Consumer 内同一 alert_hash 的并发分析。
    当重复 webhook 同时抵达时，多个 Worker 可能从 Redis Stream
    拉取到相同 alert_hash 的不同消息。此锁确保：
    - 仅一个 Worker 获得锁并执行昂贵的 AI 分析
    - 其他 Worker (got_lock=False) 通过 Pub/Sub 等待分析结果复用

    非保护范围：此锁不用于 MQ Consumer 与 Recovery Poller 的跨路径互斥。
    Recovery Poller 仅扫描 created_at 超过阈值（默认 300s）的僵尸事件，
    与 MQ Consumer 的实时消费天然时间隔离。

    实现：Redis SET NX EX + Watchdog 自动续期（TTL/3 间隔）。
    释放：Lua 脚本原子检查 value 后 DEL，防止误删他人锁。

    Yields:
        ProcessingLockResult
    """
    from core.distributed_lock import DistributedLock
    from core.redis_client import get_redis

    threshold = max(0, int(Config.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD))
    window_seconds = max(1, int(Config.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS))
    queue_key = f"queue:webhook:{alert_hash}"
    queue_size = 0
    suppressed = False

    if threshold:
        try:
            redis = get_redis()
            queue_size = int(await redis.incr(queue_key))
            if queue_size == 1:
                await redis.expire(queue_key, window_seconds)
            if queue_size > threshold:
                suppressed = True
        except Exception as e:
            logger.warning("processing_lock 计数失败: %s", e)

    lock_key = f"lock:webhook:{alert_hash}"
    ttl = Config.retry.PROCESSING_LOCK_TTL_SECONDS
    lock = DistributedLock(key=lock_key, ttl=ttl)
    lock_acquired = False

    try:
        if not suppressed:
            lock_acquired = await lock.acquire()
            if lock_acquired:
                logger.debug("[Lock] 成功锁定告警: hash=%s, worker=%s", alert_hash, Config.server.WORKER_ID)
            else:
                logger.debug("告警正由其他 worker 处理中: hash=%s...", alert_hash[:16])
    except Exception as e:
        lock_acquired = False
        logger.error("获取处理锁失败: %s", e)

    try:
        yield ProcessingLockResult(
            got_lock=lock_acquired,
            should_wait=not suppressed and not lock_acquired,
            suppressed=suppressed,
            queue_size=queue_size,
        )
    finally:
        await lock.release()
        if lock_acquired:
            logger.debug("释放处理锁: hash=%s...", alert_hash[:16])


@dataclass(frozen=True)
class ProcessingLockResult:
    got_lock: bool
    should_wait: bool
    suppressed: bool
    queue_size: int = 0
