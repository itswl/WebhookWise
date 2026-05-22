"""Sensitive data redaction helpers for persisted and API-visible payloads."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from core import json

REDACTED = "[REDACTED]"

SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "x-api-key",
    "x-auth-token",
    "x-gitlab-token",
    "x-hub-signature",
    "x-hub-signature-256",
    "x-slack-signature",
    "x-webhook-signature",
}

SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "passwd",
    "password",
    "private_key",
    "secret",
    "signature",
    "token",
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of HTTP headers with credential-like fields redacted."""
    if not headers:
        return {}
    redacted: dict[str, Any] = {}
    for key, value in headers.items():
        redacted[key] = REDACTED if str(key).lower() in SENSITIVE_HEADER_NAMES else value
    return redacted


def redact_nested(value: Any) -> Any:
    """Recursively redact sensitive fields from JSON-like data."""
    if isinstance(value, Mapping):
        return {str(k): REDACTED if _is_sensitive_key(k) else redact_nested(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_nested(v) for v in value]
    if isinstance(value, tuple):
        return [redact_nested(v) for v in value]
    return value


def redact_raw_payload_text(raw_payload: str | None) -> str | None:
    """Redact API-visible raw payload text.

    JSON payloads are returned with sensitive keys masked. Non-JSON payloads are
    not echoed back because they have no field boundaries we can reliably redact.
    """
    if raw_payload is None:
        return None
    if raw_payload == "":
        return ""
    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError:
        digest = hashlib.sha256(raw_payload.encode("utf-8", errors="replace")).hexdigest()
        return f"[REDACTED_NON_JSON_PAYLOAD size={len(raw_payload.encode('utf-8'))} sha256={digest}]"
    return json.dumps(redact_nested(parsed))


def redact_event_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive fields in a serialized WebhookEvent dictionary."""
    redacted = dict(data)
    redacted["headers"] = redact_headers(redacted.get("headers") if isinstance(redacted.get("headers"), dict) else {})
    raw_payload = redacted.get("raw_payload")
    if raw_payload is None or isinstance(raw_payload, str):
        redacted["raw_payload"] = redact_raw_payload_text(raw_payload)
    else:
        redacted["raw_payload"] = REDACTED
    if isinstance(redacted.get("parsed_data"), dict):
        redacted["parsed_data"] = redact_nested(redacted["parsed_data"])
    return redacted
