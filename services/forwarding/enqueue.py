from __future__ import annotations

from typing import Any

from services.forwarding.outbox import create_external_outbox_record, schedule_forward_outbox_many


async def enqueue_external_message(
    *,
    channel_name: str,
    target_url: str,
    event_type: str,
    formatted_payload: dict[str, Any],
    webhook_id: int | None = None,
    idempotency_hint: str = "",
) -> int:
    outbox_id = await create_external_outbox_record(
        channel_name=channel_name,
        target_url=target_url,
        event_type=event_type,
        formatted_payload=formatted_payload,
        webhook_id=webhook_id,
        idempotency_hint=idempotency_hint,
    )
    await schedule_forward_outbox_many([outbox_id])
    return outbox_id

