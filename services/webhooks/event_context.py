from __future__ import annotations

import copy
from typing import Any

from adapters.ecosystem_adapters import normalize_webhook_event
from core.datetime_utils import utc_isoformat
from models import WebhookEvent
from services.webhooks.repository import load_event_payload

WebhookEventContext = dict[str, Any]


async def build_webhook_context(event: WebhookEvent) -> WebhookEventContext:
    parsed_data, _ = await load_event_payload(event)
    source = event.source
    if (not source or source == "unknown") and isinstance(parsed_data, dict):
        # Deep-copy before re-normalizing: parsed_data may be the ORM-attached
        # JSONB value, and adapters share (not copy) the input tree since the
        # hot path owns its json.loads output. This branch is a rare fallback
        # (unknown source on replay), so the defensive copy is cheap here.
        normalized = normalize_webhook_event(copy.deepcopy(parsed_data), None)
        source, parsed_data = normalized.source or source, dict(normalized.data)
    return {
        "source": source,
        "parsed_data": parsed_data,
        "timestamp": utc_isoformat(event.timestamp),
        "client_ip": event.client_ip,
    }
