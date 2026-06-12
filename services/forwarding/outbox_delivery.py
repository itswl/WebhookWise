"""Delivery execution for forwarding outbox records."""

from __future__ import annotations

from models import ForwardOutbox
from services.forwarding.channels import resolve_channel
from services.webhooks.types import ForwardResult, is_pending_result


def _is_forward_success(result: ForwardResult) -> bool:
    return result.get("status") == "success" or is_pending_result(result)


async def deliver_outbox_record(record: ForwardOutbox) -> ForwardResult:
    # Channel-specific dispatch (openclaw / feishu / generic webhook) lives in
    # the ForwardChannel registry; this just resolves and delegates.
    return await resolve_channel(record).deliver(record)
