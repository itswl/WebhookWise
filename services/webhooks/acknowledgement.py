"""Alert acknowledgement: mark an alert chain as "I'm on it".

Acknowledging suppresses the recurring periodic reminder for the alert while
leaving the first notification and the cooldown untouched. The periodic reminder
keys off the alert-chain head (the original event), so an ack is always applied
to that head — acknowledging any occurrence in a dedup chain mutes the chain.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from models import WebhookEvent

logger = get_logger("webhooks.acknowledgement")


async def _resolve_chain_head(session: AsyncSession, webhook_id: int) -> WebhookEvent | None:
    event = await session.get(WebhookEvent, webhook_id)
    if event is None:
        return None
    if event.duplicate_of is not None:
        head = await session.get(WebhookEvent, event.duplicate_of)
        if head is not None:
            return head
    return event


async def acknowledge_webhook(
    session: AsyncSession, webhook_id: int, *, acknowledged_by: str = ""
) -> WebhookEvent | None:
    """Acknowledge an alert chain. First-ack-wins (idempotent).

    Returns the chain-head event (already acknowledged or freshly acknowledged),
    or None if no such event exists.
    """
    head = await _resolve_chain_head(session, webhook_id)
    if head is None:
        return None
    if head.acknowledged_at is None:
        head.acknowledged_at = utcnow()
        head.acknowledged_by = acknowledged_by or None
        await session.flush()
        logger.info(
            "[Ack] Alert acknowledged event_id=%s head_id=%s by=%s",
            webhook_id,
            head.id,
            acknowledged_by or "",
        )
    return head


async def unacknowledge_webhook(session: AsyncSession, webhook_id: int) -> WebhookEvent | None:
    """Clear acknowledgement on an alert chain (re-enable the periodic reminder).

    Returns the chain-head event, or None if no such event exists.
    """
    head = await _resolve_chain_head(session, webhook_id)
    if head is None:
        return None
    if head.acknowledged_at is not None:
        head.acknowledged_at = None
        head.acknowledged_by = None
        await session.flush()
        logger.info("[Ack] Alert acknowledgement cleared event_id=%s head_id=%s", webhook_id, head.id)
    return head
