"""Canonical OpenTelemetry attribute names used by WebhookWise."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SERVICE_NAME = "service.name"
SERVICE_VERSION = "service.version"
DEPLOYMENT_ENVIRONMENT = "deployment.environment"
SERVICE_INSTANCE_ID = "service.instance.id"

WEBHOOK_SOURCE = "webhook.source"
WEBHOOK_EVENT_ID = "webhook.event_id"
WEBHOOK_ALERT_HASH = "webhook.alert_hash"
WEBHOOK_IMPORTANCE = "webhook.importance"
WEBHOOK_RELATION = "webhook.relation"
WEBHOOK_SUPPRESSED = "webhook.suppressed"
WEBHOOK_OUTCOME = "webhook.outcome"
WEBHOOK_STATUS = "webhook.status"
WEBHOOK_FIELD = "webhook.field"

FORWARD_TARGET = "forward.target"
FORWARD_STATUS = "forward.status"

AI_MODEL = "ai.model"
AI_PROVIDER = "ai.provider"
AI_ENGINE = "ai.engine"
AI_TOKEN_TYPE = "ai.token_type"

RETRY_COUNT = "retry.count"
ERROR_TYPE = "error.type"
ERROR_REASON = "error.reason"

_ALIASES = {
    "source": WEBHOOK_SOURCE,
    "event_id": WEBHOOK_EVENT_ID,
    "alert_hash": WEBHOOK_ALERT_HASH,
    "importance": WEBHOOK_IMPORTANCE,
    "relation": WEBHOOK_RELATION,
    "suppressed": WEBHOOK_SUPPRESSED,
    "outcome": WEBHOOK_OUTCOME,
    "processing_status": WEBHOOK_STATUS,
    "status": WEBHOOK_STATUS,
    "field": WEBHOOK_FIELD,
    "target": FORWARD_TARGET,
    "target_url": FORWARD_TARGET,
    "model": AI_MODEL,
    "provider": AI_PROVIDER,
    "engine": AI_ENGINE,
    "token_type": AI_TOKEN_TYPE,
    "retry_count": RETRY_COUNT,
    "error_type": ERROR_TYPE,
    "type": ERROR_TYPE,
    "reason": ERROR_REASON,
    "name": "scheduler.task.name",
    "stream": "queue.stream",
    "group": "queue.group",
    "operation": "queue.operation",
}


def normalize_attribute_key(key: str) -> str:
    """Return the canonical OTel key for a local shorthand."""
    cleaned = str(key or "").strip()
    if not cleaned:
        return cleaned
    return _ALIASES.get(cleaned, cleaned)


def normalize_attribute_value(value: object) -> str | bool | int | float:
    """Constrain values to scalar OTel attribute types."""
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)


def normalize_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str | bool | int | float]:
    if not attributes:
        return {}
    normalized: dict[str, str | bool | int | float] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        attr_key = normalize_attribute_key(key)
        if not attr_key:
            continue
        normalized[attr_key] = normalize_attribute_value(value)
    return normalized
