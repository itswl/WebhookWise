import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any

import httpx

from core.config import Config
from core.logger import logger

WebhookData = dict[str, Any]


class CircuitState(Enum):
    CLOSED = "closed"  # 正常，允许请求通过
    OPEN = "open"  # 熔断，拒绝所有请求
    HALF_OPEN = "half_open"  # 半开，允许试探请求


class CircuitBreaker:
    """
    熔断器实现，防止级联故障。

    - CLOSED（正常）：请求通过，失败计数；达到阈值后转为 OPEN
    - OPEN（熔断）：请求直接拒绝（返回 None），超时后转为 HALF_OPEN
    - HALF_OPEN（半开）：允许一个试探请求；成功则回 CLOSED，失败则回 OPEN
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple = (httpx.RequestError,),
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions

        # 异步锁（惰性初始化，绑定到当前事件循环）
        self._async_lock: asyncio.Lock | None = None

        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._state = CircuitState.CLOSED

    def _get_async_lock(self) -> asyncio.Lock:
        """获取或创建异步锁（惰性初始化，绑定到当前事件循环）"""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    @property
    def state(self) -> CircuitState:
        """读取当前熔断器状态（仅供外部查询，不持锁）。"""
        return self._state

    async def _check_state(self) -> CircuitState:
        """在 asyncio.Lock 保护下检查并转换状态。"""
        async with self._get_async_lock():
            if (
                self._state == CircuitState.OPEN
                and self._last_failure_time is not None
                and time.time() - self._last_failure_time >= self.recovery_timeout
            ):
                self._state = CircuitState.HALF_OPEN
            return self._state

    async def call_async(self, func: Callable, *args, **kwargs):
        """异步执行函数，失败时触发熔断（使用 asyncio.Lock 保护所有状态访问）。"""
        if self.failure_threshold == 0:
            try:
                return await func(*args, **kwargs)
            except self.expected_exceptions as e:
                logger.warning(f"CircuitBreaker [{self.name}] 请求异常（已禁用）: {e}")
                return None

        current_state = await self._check_state()
        if current_state == CircuitState.OPEN:
            logger.warning(f"CircuitBreaker [{self.name}] OPEN — 请求被拒绝")
            return None

        try:
            result = await func(*args, **kwargs)
            async with self._get_async_lock():
                self._failure_count = 0
                self._state = CircuitState.CLOSED
            return result
        except self.expected_exceptions as e:
            async with self._get_async_lock():
                self._failure_count += 1
                self._last_failure_time = time.time()
                if self.failure_threshold > 0 and self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.error(
                        f"CircuitBreaker [{self.name}] 触发熔断: "
                        f"连续失败 {self._failure_count} 次, "
                        f"将在 {self.recovery_timeout}s 后恢复"
                    )
            logger.warning(f"CircuitBreaker [{self.name}] 请求异常: {e}")
            return None


# 预置熔断器实例（通过 Config）

feishu_cb = CircuitBreaker(
    name="feishu",
    failure_threshold=Config.CIRCUIT_BREAKER_FEISHU_THRESHOLD,
    recovery_timeout=Config.CIRCUIT_BREAKER_FEISHU_TIMEOUT,
)
openclaw_cb = CircuitBreaker(
    name="openclaw",
    failure_threshold=Config.CIRCUIT_BREAKER_OPENCLAW_THRESHOLD,
    recovery_timeout=Config.CIRCUIT_BREAKER_OPENCLAW_TIMEOUT,
)
forward_cb = CircuitBreaker(
    name="forward",
    failure_threshold=Config.CIRCUIT_BREAKER_FORWARD_THRESHOLD,
    recovery_timeout=Config.CIRCUIT_BREAKER_FORWARD_TIMEOUT,
)


HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]


MAX_SAVE_RETRIES = Config.SAVE_MAX_RETRIES
RETRY_DELAY_SECONDS = Config.SAVE_RETRY_DELAY_SECONDS


def verify_signature(payload: bytes, signature: str, secret: str | None = None) -> bool:
    """验证 webhook 签名"""
    if secret is None:
        secret = Config.WEBHOOK_SECRET

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

    key_string = json.dumps(key_fields, sort_keys=True, ensure_ascii=False)
    hash_value = hashlib.sha256(key_string.encode("utf-8")).hexdigest()

    logger.debug(f"[Hash] 生成告警哈希: hash={hash_value[:16]}..., input_keys={list(key_fields.keys())}")
    return hash_value


@asynccontextmanager
async def processing_lock(alert_hash: str) -> AsyncGenerator[bool, None]:
    """
    告警处理锁上下文管理器（Redis 分布式锁）

    利用 Redis SET NX EX 防止多 worker 并发处理同一告警。
    """
    import core.redis_client

    redis_client = core.redis_client.get_redis()
    lock_key = f"lock:webhook:{alert_hash}"
    lock_value = Config.WORKER_ID

    lock_acquired = False

    try:
        # 尝试获取锁
        lock_acquired = bool(
            await redis_client.set(lock_key, lock_value, nx=True, ex=Config.PROCESSING_LOCK_TTL_SECONDS)
        )
        if lock_acquired:
            logger.debug(f"[Lock] 成功锁定告警: hash={alert_hash}, worker={Config.WORKER_ID}")
        else:
            logger.debug(f"告警正由其他 worker 处理中: hash={alert_hash[:16]}...")
    except Exception as e:
        logger.error(f"获取处理锁失败: {e}")

    try:
        yield lock_acquired
    finally:
        if lock_acquired:
            try:
                release_lua = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                else
                    return 0
                end
                """
                await redis_client.eval(release_lua, 1, lock_key, lock_value)
                logger.debug(f"释放处理锁: hash={alert_hash[:16]}...")
            except Exception as e:
                logger.error(f"释放锁失败: {e}")
