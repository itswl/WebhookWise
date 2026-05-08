from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
        return {key: normalized for key, value in values.items() if (normalized := _normalize_identity_value(value))}


def with_alert_identity(data: dict[str, Any], identity: AlertIdentity) -> dict[str, Any]:
    normalized = dict(data)
    normalized[IDENTITY_FIELD] = identity.to_payload()
    return normalized


def extract_alert_identity(data: dict[str, Any]) -> dict[str, str] | None:
    value = data.get(IDENTITY_FIELD)
    if not isinstance(value, dict):
        return None
    identity = {
        str(key): normalized for key, raw in value.items() if (normalized := _normalize_identity_value(raw)) is not None
    }
    return identity or None
