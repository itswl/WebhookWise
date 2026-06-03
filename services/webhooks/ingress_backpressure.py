"""Ingress backpressure before PostgreSQL writes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from redis.exceptions import RedisError

from adapters.ecosystem_adapters import normalize_webhook_event
from core import json
from core.logger import get_logger
from core.observability.metrics import REDIS_UNAVAILABLE_TOTAL
from services.dedup import generate_alert_hash
from services.webhooks.policies import IngressPolicy

logger = get_logger("ingress_backpressure")

_INGRESS_COUNTER_LUA = """
local c = redis.call("incr", KEYS[1])
if c == 1 then
    redis.call("expire", KEYS[1], tonumber(ARGV[1]))
end
return c
"""


@dataclass(frozen=True, slots=True)
class IngressBackpressureResult:
    suppressed: bool
    key: str
    count: int
    threshold: int
    reason: str = ""


def _fallback_body_hash(source: str, raw_body: bytes) -> str:
    digest = hashlib.sha256(source.encode("utf-8") + b"\0" + raw_body).hexdigest()
    return f"body:{digest}"


def _ingress_identity(source_hint: str, raw_body: bytes) -> str:
    try:
        loaded = json.loads(raw_body)
        payload = loaded if isinstance(loaded, dict) else {}
        if not payload:
            return _fallback_body_hash(source_hint, raw_body)
        normalized = normalize_webhook_event(payload, source_hint)
        return f"alert:{generate_alert_hash(dict(normalized.data), normalized.source)}"
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        logger.debug("[IngressBackpressure] 无法解析 ingress identity，使用 body hash: %s", e)
        return _fallback_body_hash(source_hint, raw_body)


async def check_ingress_backpressure(
    *,
    source_hint: str,
    raw_body: bytes,
    policy: IngressPolicy | None = None,
    redis_eval_int_func: Any | None = None,
) -> IngressBackpressureResult:
    """Return whether this request should be dropped before any DB write."""
    policy = policy or IngressPolicy.from_config()
    threshold = policy.ingress_backpressure_threshold
    if threshold <= 0:
        return IngressBackpressureResult(False, "", 0, threshold)

    identity = _ingress_identity(source_hint, raw_body)
    key = f"ingress:webhook:{identity}"
    try:
        if redis_eval_int_func is None:
            from core.redis_client import redis_eval_int
            from core.redis_health import ensure_redis_available

            if not await ensure_redis_available("ingress_backpressure:counter"):
                if policy.ingress_backpressure_fail_open_on_redis_error:
                    REDIS_UNAVAILABLE_TOTAL.labels("ingress_backpressure", "allowed").inc()
                    logger.warning("[IngressBackpressure] Redis 不可用，ingress 降级放行 key=%s", key)
                    return IngressBackpressureResult(False, key, 0, threshold, reason="redis_unavailable_fail_open")
                REDIS_UNAVAILABLE_TOTAL.labels("ingress_backpressure", "suppressed").inc()
                logger.warning("[IngressBackpressure] Redis 不可用，ingress 按背压抑制 key=%s", key)
                return IngressBackpressureResult(True, key, 0, threshold, reason="redis_unavailable")

            redis_eval_int_func = redis_eval_int
        raw_count = await redis_eval_int_func(_INGRESS_COUNTER_LUA, 1, key, policy.ingress_backpressure_window_seconds)
        if raw_count is None:
            raise RuntimeError("ingress counter script returned no integer")
        count = int(raw_count)
    except (RedisError, RuntimeError, TypeError, ValueError) as e:
        from core.redis_health import mark_redis_failure

        mark_redis_failure("ingress_backpressure:counter", e)
        if policy.ingress_backpressure_fail_open_on_redis_error:
            REDIS_UNAVAILABLE_TOTAL.labels("ingress_backpressure", "allowed").inc()
            logger.warning("[IngressBackpressure] Redis 计数失败，ingress 降级放行: %s", e)
            return IngressBackpressureResult(False, key, 0, threshold, reason="redis_unavailable_fail_open")
        REDIS_UNAVAILABLE_TOTAL.labels("ingress_backpressure", "suppressed").inc()
        logger.warning("[IngressBackpressure] Redis 计数失败，ingress 按背压抑制: %s", e)
        return IngressBackpressureResult(True, key, 0, threshold, reason="redis_unavailable")

    suppressed = count > threshold
    return IngressBackpressureResult(
        suppressed=suppressed,
        key=key,
        count=count,
        threshold=threshold,
        reason="ingress_storm_backpressure" if suppressed else "",
    )
