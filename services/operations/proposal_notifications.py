"""Durable Feishu intent for a pending remediation proposal, with decision buttons.

The dashboard queue works, but the operator lives in Feishu — the alerts that
justify a proposal already arrive there. This mirrors the incident-card path:
one idempotent ForwardOutbox intent per proposal, buttons only on the app
channel where a verified operator identity can come back, plain card elsewhere.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from models import ForwardOutbox, RemediationProposal
from services.forwarding.policies import ForwardDeliveryPolicy
from services.notifications.feishu_actions import build_proposal_action_value
from services.notifications.routing import resolve_notification_target
from services.webhooks.types import ForwardOutboxStatus

_MAX_REASON_PREVIEW = 300


def _proposal_card(
    proposal: RemediationProposal,
    *,
    interactive_actions: bool,
    dashboard_url: str,
) -> dict[str, Any]:
    reason = (proposal.reason or "").strip()
    if len(reason) > _MAX_REASON_PREVIEW:
        reason = reason[: _MAX_REASON_PREVIEW - 1] + "…"
    resource = (
        f"{proposal.resource_type} #{proposal.resource_id}"
        if proposal.resource_type and proposal.resource_id
        else "global"
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**Action:** {proposal.action}\n"
                f"**Resource:** {resource}\n"
                f"**Proposed by:** {proposal.proposed_by}\n"
                f"**Expires:** {proposal.expires_at.isoformat()}\n"
                f"**Reason:** {reason}"
            ),
        }
    ]
    if interactive_actions:
        # A decision button must never outlive the proposal it decides: the
        # signed value's expiry is the EARLIER of the card-action TTL and the
        # proposal's own expiry.
        expiry_epoch = min(
            int(time.time()) + int(get_config_manager().notifications.FEISHU_CARD_ACTION_TTL_SECONDS),
            int(proposal.expires_at.timestamp()),
        )
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Approve & execute"},
                        "type": "danger",
                        "confirm": {
                            "title": {"tag": "plain_text", "content": "Execute this remediation?"},
                            "text": {
                                "tag": "plain_text",
                                "content": f"This runs {proposal.action} against {resource} now, recorded as you.",
                            },
                        },
                        "value": build_proposal_action_value("approve", int(proposal.id), expires_at=expiry_epoch),
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Reject"},
                        "type": "default",
                        "confirm": {
                            "title": {"tag": "plain_text", "content": "Reject this proposal?"},
                            "text": {
                                "tag": "plain_text",
                                "content": "The command will not run. The proposer can see the rejection.",
                            },
                        },
                        "value": build_proposal_action_value("reject", int(proposal.id), expires_at=expiry_epoch),
                    },
                ],
            }
        )
    if dashboard_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Open WebhookWise"},
                        "type": "default",
                        "url": dashboard_url,
                    }
                ],
            }
        )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {
                "enable_forward": False,
                "update_multi": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"⚙️ Remediation proposal #{proposal.id} awaits approval",
                }
            },
            "elements": elements,
        },
    }


async def queue_proposal_notification(
    session: AsyncSession,
    proposal: RemediationProposal,
) -> int | None:
    """Insert one idempotent Feishu intent for a freshly proposed remediation.

    Buttons render only when the full card-action configuration is present —
    the same six-condition check the incident card uses — because a plain
    webhook card has no verified operator identity to send back.
    """
    app_config = get_config_manager()
    cfg = app_config.notifications
    app_enabled = bool(
        cfg.FEISHU_CARD_ACTIONS_ENABLED
        and cfg.FEISHU_APP_ID.strip()
        and cfg.FEISHU_APP_SECRET.strip()
        and cfg.FEISHU_INCIDENT_CHAT_ID.strip()
        and app_config.security.FEISHU_CARD_VERIFICATION_TOKEN.strip()
        and app_config.security.FEISHU_CARD_ACTION_SECRET.strip()
    )
    configured = (
        f"feishu-app://{cfg.FEISHU_INCIDENT_CHAT_ID.strip()}"
        if app_enabled
        else str(cfg.DEEP_ANALYSIS_FEISHU_WEBHOOK or cfg.WEEKLY_REPORT_FEISHU_WEBHOOK or "").strip()
    )
    target = await resolve_notification_target(
        "remediation_proposed",
        fallback_url=configured,
        fallback_name="remediation-proposal-notification",
        fallback_target_type="feishu_app" if app_enabled else "feishu",
    )
    if not target.url and target.target_type != "feishu_app":
        return None

    key = f"remediation-proposal:{proposal.id}"
    existing = (
        await session.execute(select(ForwardOutbox.id).where(ForwardOutbox.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)

    policy = ForwardDeliveryPolicy.from_config()
    now = utcnow()
    record = ForwardOutbox(
        idempotency_key=key,
        webhook_event_id=None,
        original_event_id=None,
        forward_rule_id=target.rule_id,
        rule_name=target.rule_name if target.from_rule else "system:remediation-proposed",
        target_type=target.target_type,
        target_url=target.url,
        target_name=target.rule_name,
        channel_name=target.target_type,
        event_type="remediation_proposed",
        status=ForwardOutboxStatus.PENDING,
        attempts=0,
        max_attempts=policy.max_attempts,
        next_attempt_at=now,
        formatted_payload=_proposal_card(
            proposal,
            interactive_actions=app_enabled,
            dashboard_url=str(cfg.DASHBOARD_PUBLIC_URL or "").strip(),
        ),
        created_at=now,
        updated_at=now,
    )
    session.add(record)
    await session.flush()
    FORWARD_OUTBOX_RECORDS_TOTAL.labels("feishu_app" if app_enabled else "feishu", "created").inc()
    return int(record.id)
