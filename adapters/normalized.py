from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contracts.webhook_payload import WebhookData, webhook_data_from_mapping

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
    # copy=False: adapters normalize freshly-parsed request bodies (json.loads
    # yields a strict tree with a single owner), so the boundary validation
    # runs in full while the recursive container rebuild — the dominant
    # normalization cost per alert, paid on the synchronous ingress path too —
    # is skipped. The one caller that re-normalizes ORM-loaded JSONB guards
    # itself with a deep copy (services/webhooks/event_context.py).
    return webhook_data_from_mapping(normalized, strict=False, copy=False)


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
