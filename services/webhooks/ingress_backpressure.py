"""Ingress backpressure before PostgreSQL writes."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import orjson

from adapters.ecosystem_adapters import normalize_webhook_event
from models import WebhookEvent
from services.webhooks.policies import WebhookReceivePolicy

logger = logging.getLogger("webhook_service.ingress_backpressure")

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
        loaded = orjson.loads(raw_body)
        payload = loaded if isinstance(loaded, dict) else {}
        if not payload:
            return _fallback_body_hash(source_hint, raw_body)
        normalized = normalize_webhook_event(payload, source_hint)
        return f"alert:{WebhookEvent.generate_hash(normalized.data, normalized.source)}"
    except Exception as e:
        logger.debug("[IngressBackpressure] 无法解析 ingress identity，使用 body hash: %s", e)
        return _fallback_body_hash(source_hint, raw_body)


async def check_ingress_backpressure(
    *,
    source_hint: str,
    raw_body: bytes,
    policy: WebhookReceivePolicy | None = None,
    redis_eval_int_func: Any | None = None,
) -> IngressBackpressureResult:
    """Return whether this request should be dropped before any DB write."""
    policy = policy or WebhookReceivePolicy.from_config()
    threshold = policy.ingress_backpressure_threshold
    if threshold <= 0:
        return IngressBackpressureResult(False, "", 0, threshold)

    identity = _ingress_identity(source_hint, raw_body)
    key = f"ingress:webhook:{identity}"
    try:
        if redis_eval_int_func is None:
            from core.redis_client import redis_eval_int

            redis_eval_int_func = redis_eval_int
        count = int(await redis_eval_int_func(_INGRESS_COUNTER_LUA, 1, key, policy.ingress_backpressure_window_seconds))
    except Exception as e:
        logger.warning("[IngressBackpressure] Redis 计数失败，降级放行: %s", e)
        return IngressBackpressureResult(False, key, 0, threshold)

    suppressed = count > threshold
    return IngressBackpressureResult(
        suppressed=suppressed,
        key=key,
        count=count,
        threshold=threshold,
        reason="ingress_storm_backpressure" if suppressed else "",
    )
