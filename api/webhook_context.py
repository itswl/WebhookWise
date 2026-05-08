from typing import Any

from adapters.ecosystem_adapters import normalize_webhook_event
from models import WebhookEvent

JSONDict = dict[str, Any]


async def build_webhook_context(event: WebhookEvent) -> JSONDict:
    from services.webhooks.pipeline import _load_event_payload

    parsed_data, _ = await _load_event_payload(event)
    source = event.source
    if (not source or source == "unknown") and isinstance(parsed_data, dict):
        normalized = normalize_webhook_event(parsed_data, None)
        source, parsed_data = normalized.source or source, normalized.data
    return {
        "source": source,
        "parsed_data": parsed_data,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "client_ip": event.client_ip,
    }
