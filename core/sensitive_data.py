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
    "x-admin-key",
    "x-admin-write-key",
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


def _mask_secret(value: str) -> str:
    """Mask a secret, keeping the last 4 chars for recognizability (`****1234`)."""
    if not value:
        return value
    return ("****" + value[-4:]) if len(value) > 4 else "****"


def mask_webhook_url(url: str | None) -> str | None:
    """Mask the secret token in an outbound webhook URL for display.

    Keeps the scheme/host/path shape recognizable but redacts the credential:
    - bot-hook style: ``.../hook/<token>`` / ``.../send/<token>`` / ``.../webhook/<token>``
      (Feishu, WeCom, generic) → mask the segment right after the keyword
      (NOT version segments like ``/bot/v2/``).
    - query-token style: ``?access_token=...`` / ``?token=...`` / ``?key=...``
      (DingTalk, others) → mask those query values.
    The masked value keeps the last 4 chars so an operator can still tell two
    targets apart without exposing the full secret. Best-effort: an unparseable
    URL is hard-masked.
    """
    if not url:
        return url
    import re
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"

    # Mask the single path segment immediately after a secret-bearing keyword.
    path = re.sub(
        r"(/(?:hook|send|webhook)/)([^/?#]+)",
        lambda m: m.group(1) + _mask_secret(m.group(2)),
        parts.path,
        flags=re.IGNORECASE,
    )

    # Query-string secrets. Rebuild manually (not urlencode) so the mask's '*'
    # chars are not percent-encoded into noise.
    if parts.query:
        masked_pairs = [
            f"{k}={_mask_secret(v) if (_is_sensitive_key(k) or k.lower() in ('access_token', 'key')) else v}"
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        query = "&".join(masked_pairs)
    else:
        query = parts.query

    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of HTTP headers with credential-like fields redacted."""
    if not headers:
        return {}
    redacted: dict[str, Any] = {}
    for key, value in headers.items():
        normalized = str(key).lower()
        redacted[key] = REDACTED if normalized in SENSITIVE_HEADER_NAMES or _is_sensitive_key(key) else value
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
