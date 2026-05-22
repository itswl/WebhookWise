"""Webhook request parsing for the processing pipeline."""

from typing import Any

from adapters.ecosystem_adapters import normalize_webhook_event
from core import json
from services.webhooks.types import WebhookRequestContext


def parse_request(
    client_ip: str,
    headers: dict[str, Any],
    payload: dict[str, Any],
    raw_body: bytes,
    source: str | None,
    ts: str | None,
) -> WebhookRequestContext:
    src = source or headers.get("x-webhook-source", "unknown")
    if not payload and raw_body:
        loaded = json.loads(raw_body)
        payload = loaded if isinstance(loaded, dict) else {}
    norm = normalize_webhook_event(payload, src)
    return WebhookRequestContext(
        client_ip=client_ip,
        source=norm.source,
        payload=raw_body,
        parsed_data=norm.data,
        webhook_full_data={
            "body": payload,
            "headers": headers,
            "parsed_data": norm.data,
            "source": norm.source,
            "timestamp": ts,
        },
        headers=headers,
    )
