from __future__ import annotations

import orjson

from core.compression import decompress_payload_async
from models import WebhookEvent


async def load_event_payload(
    event: WebhookEvent,
) -> tuple[dict | None, str]:
    raw_text = await decompress_payload_async(event.raw_payload) or ""
    parsed_data = event.parsed_data
    if parsed_data is None and raw_text:
        try:
            parsed_data = orjson.loads(raw_text)
        except Exception:
            parsed_data = None
    return parsed_data, raw_text

