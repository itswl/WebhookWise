"""The proposal card: one idempotent intent, buttons only where identity can return."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ForwardOutbox
from services.operations.remediation_proposals import propose_remediation
from tests.helpers.db import ensure_forward_rules


async def _propose(session: AsyncSession) -> dict[str, Any]:
    return await propose_remediation(
        session,
        action="retry_outbox",
        resource_id=7,
        reason="outbox record 7 has been retrying for 40 minutes with the same 503",
        proposed_by="hookprobe",
    )


def _enable_app_channel(temp_config: Any) -> None:
    temp_config.notifications.FEISHU_CARD_ACTIONS_ENABLED = True
    temp_config.notifications.FEISHU_APP_ID = "cli_unit"
    temp_config.notifications.FEISHU_APP_SECRET = "unit-app-secret"
    temp_config.notifications.FEISHU_INCIDENT_CHAT_ID = "oc_unit_chat"
    temp_config.security.FEISHU_CARD_VERIFICATION_TOKEN = "unit-verification-token"
    temp_config.security.FEISHU_CARD_ACTION_SECRET = "unit-signing-secret"


@pytest.mark.asyncio
async def test_propose_queues_one_card_with_buttons_clamped_to_the_proposal(
    db_session: AsyncSession,
    temp_config: Any,
) -> None:
    _enable_app_channel(temp_config)

    proposal = await _propose(db_session)

    outbox = (
        await db_session.execute(
            select(ForwardOutbox).where(ForwardOutbox.idempotency_key == f"remediation-proposal:{proposal['id']}")
        )
    ).scalar_one()
    assert outbox.event_type == "remediation_proposed"
    assert outbox.target_type == "feishu_app"

    card = outbox.formatted_payload
    rendered = json.dumps(card)
    assert "Approve & execute" in rendered
    assert "Reject" in rendered
    # The signing secret must never ride inside the card payload.
    assert "unit-signing-secret" not in rendered

    buttons = [
        action
        for element in card["card"]["elements"]
        if element.get("tag") == "action"
        for action in element.get("actions", [])
        if "value" in action
    ]
    assert {button["value"]["action"] for button in buttons} == {"approve", "reject"}
    for button in buttons:
        value = button["value"]
        assert value["resource_type"] == "remediation_proposal"
        assert value["resource_id"] == proposal["id"]
        # A decision button must not outlive its proposal: expiry is clamped to
        # the row's expires_at, not the (days-long) card-action TTL.
        assert value["expires_at"] <= int(time.time()) + 25 * 3600

    # Re-queueing the same proposal reuses the intent instead of duplicating it.
    from models import RemediationProposal
    from services.operations.proposal_notifications import queue_proposal_notification

    row = await db_session.get(RemediationProposal, int(proposal["id"]))
    assert row is not None
    again = await queue_proposal_notification(db_session, row)
    assert again == int(outbox.id)
    assert (
        await db_session.scalar(
            select(func.count(ForwardOutbox.id)).where(ForwardOutbox.event_type == "remediation_proposed")
        )
    ) == 1


@pytest.mark.asyncio
async def test_webhook_fallback_gets_a_plain_card_without_buttons(
    db_session: AsyncSession,
    temp_config: Any,
) -> None:
    temp_config.notifications.FEISHU_CARD_ACTIONS_ENABLED = False
    temp_config.notifications.DEEP_ANALYSIS_FEISHU_WEBHOOK = "https://open.feishu.cn/hook/unit-fallback"

    proposal = await _propose(db_session)

    outbox = (
        await db_session.execute(
            select(ForwardOutbox).where(ForwardOutbox.idempotency_key == f"remediation-proposal:{proposal['id']}")
        )
    ).scalar_one()
    assert outbox.target_type == "feishu"
    rendered = json.dumps(outbox.formatted_payload)
    assert "Approve & execute" not in rendered
    assert "awaits approval" in rendered


@pytest.mark.asyncio
async def test_no_configured_target_keeps_the_proposal_and_skips_the_card(
    db_session: AsyncSession,
    temp_config: Any,
) -> None:
    temp_config.notifications.FEISHU_CARD_ACTIONS_ENABLED = False
    temp_config.notifications.DEEP_ANALYSIS_FEISHU_WEBHOOK = ""
    temp_config.notifications.WEEKLY_REPORT_FEISHU_WEBHOOK = ""

    proposal = await _propose(db_session)

    assert proposal["status"] == "pending"
    assert (
        await db_session.scalar(
            select(func.count(ForwardOutbox.id)).where(ForwardOutbox.event_type == "remediation_proposed")
        )
    ) == 0


@pytest.mark.asyncio
async def test_a_failing_card_queue_never_loses_the_proposal(
    db_session: AsyncSession,
    temp_config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shadow reviewer's first catch: a failed flush inside the queue used
    to leave the session pending-rollback, so the commit that followed raised
    and rolled the just-inserted proposal back. The savepoint contains it."""
    import services.operations.proposal_notifications as notifications

    async def explode(session: AsyncSession, proposal: Any) -> int:
        session.add(ForwardOutbox())  # violates NOT NULLs -> failed flush
        await session.flush()
        raise AssertionError("unreachable")

    monkeypatch.setattr(notifications, "queue_proposal_notification", explode)

    proposal = await _propose(db_session)

    assert proposal["status"] == "pending"
    from models import RemediationProposal

    row = await db_session.get(RemediationProposal, int(proposal["id"]))
    assert row is not None and row.status == "pending"
    assert (
        await db_session.scalar(
            select(func.count(ForwardOutbox.id)).where(ForwardOutbox.event_type == "remediation_proposed")
        )
    ) == 0


@pytest.mark.asyncio
async def test_a_rule_claiming_a_webhook_target_strips_the_buttons(
    db_session: AsyncSession,
    temp_config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App fully configured, but a forward rule routes the event to a plain
    webhook: decision buttons must not ride a channel that cannot call back."""
    import services.operations.proposal_notifications as notifications
    from services.notifications.routing import NotificationTarget

    _enable_app_channel(temp_config)

    async def rule_claims_webhook(event_type: str, **_kwargs: Any) -> NotificationTarget:
        return NotificationTarget(
            url="https://open.feishu.cn/hook/ops-rule",
            target_type="feishu",
            rule_id=5,
            rule_name="ops-rule",
        )

    monkeypatch.setattr(notifications, "resolve_notification_target", rule_claims_webhook)

    # The proposal card is filed against rule 5; forward_outboxes.forward_rule_id
    # is a real foreign key that SQLite never enforced.
    await ensure_forward_rules(db_session, 5)
    proposal = await _propose(db_session)

    outbox = (
        await db_session.execute(
            select(ForwardOutbox).where(ForwardOutbox.idempotency_key == f"remediation-proposal:{proposal['id']}")
        )
    ).scalar_one()
    assert outbox.target_type == "feishu"
    assert outbox.forward_rule_id == 5
    rendered = json.dumps(outbox.formatted_payload)
    assert "Approve & execute" not in rendered
