from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.webhooks.types import WebhookData, webhook_data_from_mapping

IDENTITY_FIELD = "_alert_identity"


def _normalize_identity_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


@dataclass(frozen=True)
class AlertIdentity:
    """Canonical identity used for duplicate detection."""

    source: str
    name: str | None = None
    resource: str | None = None
    service: str | None = None
    fingerprint: str | None = None
    severity: str | None = None

    def to_payload(self) -> dict[str, str]:
        values = {
            "source": self.source,
            "name": self.name,
            "resource": self.resource,
            "service": self.service,
            "fingerprint": self.fingerprint,
            "severity": self.severity,
        }
        payload: dict[str, str] = {}
        for key, value in values.items():
            normalized = _normalize_identity_value(value)
            if normalized is not None:
                payload[key] = normalized
        return payload


def with_alert_identity(data: Mapping[str, Any], identity: AlertIdentity) -> WebhookData:
    normalized = dict(data)
    normalized[IDENTITY_FIELD] = identity.to_payload()
    return webhook_data_from_mapping(normalized, strict=False)


def extract_alert_identity(data: Mapping[str, Any]) -> dict[str, str] | None:
    value = data.get(IDENTITY_FIELD)
    if not isinstance(value, dict):
        return None
    identity: dict[str, str] = {}
    for key, raw in value.items():
        normalized = _normalize_identity_value(raw)
        if normalized is not None:
            identity[str(key)] = normalized
    return identity or None
