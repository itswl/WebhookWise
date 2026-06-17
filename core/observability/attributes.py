"""Canonical OpenTelemetry attribute names used by WebhookWise."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

OTEL_SEMCONV_VERSION_DEFAULT = "1.41.0"
INSTRUMENTATION_SCOPE_NAME = "webhookwise"

SERVICE_NAME = "service.name"
SERVICE_NAMESPACE = "service.namespace"
SERVICE_VERSION = "service.version"
DEPLOYMENT_ENVIRONMENT = "deployment.environment"
SERVICE_INSTANCE_ID = "service.instance.id"

REQUEST_ID = "request.id"

WEBHOOK_SOURCE = "webhook.source"
WEBHOOK_EVENT_ID = "webhook.event_id"
WEBHOOK_EVENT_TYPE = "webhook.event_type"
WEBHOOK_ALERT_HASH = "webhook.alert_hash"
WEBHOOK_IMPORTANCE = "webhook.importance"
WEBHOOK_OUTCOME = "webhook.outcome"
WEBHOOK_STATUS = "webhook.status"
WEBHOOK_PROCESSING_DURATION_MS = "webhook.processing.duration_ms"
WEBHOOK_ROUTE = "webhook.route"

FORWARD_STATUS = "forward.status"
FORWARD_TARGET_TYPE = "forward.target_type"

AI_MODEL = "ai.model"
AI_PROVIDER = "ai.provider"
AI_ENGINE = "ai.engine"


def normalize_attribute_key(key: str) -> str:
    """Return a cleaned canonical OTel attribute key."""
    cleaned = str(key or "").strip()
    if not cleaned:
        return cleaned
    return cleaned


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
