"""Durable incident notification intents backed by the forwarding outbox."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from models import ForwardOutbox, Incident, WebhookEvent
from services.forwarding.outbox_records import find_outbox_id_by_key, insert_outbox_or_existing
from services.forwarding.policies import ForwardDeliveryPolicy
from services.notifications.feishu_actions import build_incident_action_value
from services.notifications.markdown_safety import escape_lark_md
from services.notifications.routing import resolve_notification_target
from services.webhooks.types import ForwardOutboxStatus


def _incident_card(
    incident: Incident,
    *,
    interactive_actions: bool = False,
    dashboard_url: str = "",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**Source:** {escape_lark_md(incident.source or '') or 'unknown'}\n"
                f"**Alerts:** {incident.alert_count}\n"
                f"**Started:** {incident.started_at.isoformat()}\n"
                f"**Importance:** {incident.top_importance or '?'}"
            ),
        }
    ]
    if interactive_actions:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Acknowledge"},
                        "type": "primary",
                        # A card is read on a phone, where these two buttons sit
                        # a thumb-width apart. Resolve has always confirmed;
                        # Acknowledge did not, so the miss-tap landed on the
                        # claim and quietly made someone the owner.
                        "confirm": {
                            "title": {"tag": "plain_text", "content": "Acknowledge incident?"},
                            "text": {
                                "tag": "plain_text",
                                "content": "This assigns it to you. You can undo it from WebhookWise.",
                            },
                        },
                        "value": build_incident_action_value("acknowledge", int(incident.id)),
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Resolve"},
                        "type": "danger",
                        "confirm": {
                            "title": {"tag": "plain_text", "content": "Resolve incident?"},
                            "text": {
                                "tag": "plain_text",
                                "content": "This closes the incident and records the Feishu operator.",
                            },
                        },
                        "value": build_incident_action_value("resolve", int(incident.id)),
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Silence 2h"},
                        "type": "default",
                        "confirm": {
                            "title": {"tag": "plain_text", "content": "Silence for 2 hours?"},
                            "text": {
                                "tag": "plain_text",
                                "content": "Mutes alerts matching this incident's source/project/environment. Lift it early from WebhookWise.",
                            },
                        },
                        "value": build_incident_action_value("silence_2h", int(incident.id)),
                    },
                ],
            }
        )
        elements.append(
            {
                "tag": "form",
                "name": f"incident-note-{incident.id}",
                "elements": [
                    {
                        "tag": "input",
                        "name": "note",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "Add incident evidence or a handoff note",
                        },
                    },
                    {
                        "tag": "button",
                        "name": "submit-note",
                        "text": {"tag": "plain_text", "content": "Add note"},
                        "form_action_type": "submit",
                        "value": build_incident_action_value("add_note", int(incident.id)),
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
            "header": {"title": {"tag": "plain_text", "content": f"🚨 {incident.title[:80]}"}},
            "elements": elements,
        },
    }


async def queue_incident_notifications(
    session: AsyncSession,
    incidents: list[Incident],
) -> list[int]:
    """Insert idempotent Feishu intents in the incident transaction."""
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
    # A rule may claim incident_created; the configured cascade is the fallback.
    # These cards were reaching DEEP_ANALYSIS_FEISHU_WEBHOOK only because nothing
    # else was set, and its token had been revoked for six days unnoticed.
    configured = (
        f"feishu-app://{cfg.FEISHU_INCIDENT_CHAT_ID.strip()}"
        if app_enabled
        else str(cfg.DEEP_ANALYSIS_FEISHU_WEBHOOK or cfg.WEEKLY_REPORT_FEISHU_WEBHOOK or "").strip()
    )
    target = await resolve_notification_target(
        "incident_created",
        fallback_url=configured,
        fallback_name="incident-notification",
        fallback_target_type="feishu_app" if app_enabled else "feishu",
    )
    target_url = target.url
    if not target_url and target.target_type != "feishu_app":
        return []

    policy = ForwardDeliveryPolicy.from_config()
    now = utcnow()
    outbox_ids: list[int] = []
    for incident in incidents:
        if incident.id is None:
            continue
        key = f"incident-created:{incident.id}"
        existing = await find_outbox_id_by_key(session, key)
        if existing is not None:
            outbox_ids.append(existing)
            continue
        record = ForwardOutbox(
            idempotency_key=key,
            webhook_event_id=None,
            original_event_id=None,
            forward_rule_id=target.rule_id,
            rule_name=target.rule_name if target.from_rule else "system:incident-created",
            target_type=target.target_type,
            target_url=target_url,
            target_name=target.rule_name,
            channel_name=target.target_type,
            event_type="incident_created",
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=policy.max_attempts,
            next_attempt_at=now,
            formatted_payload=_incident_card(
                incident,
                interactive_actions=app_enabled,
                dashboard_url=str(cfg.DASHBOARD_PUBLIC_URL or "").strip(),
            ),
            created_at=now,
            updated_at=now,
        )
        # Grouping can run on more than one worker; the loser of the insert race
        # adopts the winner's row instead of failing the incident transaction.
        outbox_id, created = await insert_outbox_or_existing(session, record)
        outbox_ids.append(outbox_id)
        if created:
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("feishu_app" if app_enabled else "feishu", "created").inc()
    return outbox_ids


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 3600:
        return f"{total // 60} 分钟"
    hours, minutes = total // 3600, (total % 3600) // 60
    if hours < 48:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{hours // 24} 天 {hours % 24} 小时"


def _incident_resolved_card(incident: Incident, *, resolver: str) -> dict[str, Any]:
    """One recap card that closes the loop in chat: how long, how big, who,
    and what the AI summary concluded — assembled from what already exists.
    The summary is generated asynchronously, so a fast resolve says
    "生成中" rather than waiting for the model."""
    ended = incident.resolved_at or incident.ended_at or utcnow()
    duration = _format_duration((ended - incident.started_at).total_seconds())

    lines = [
        f"**⏱️ 持续时长**：{duration}",
        f"**📊 告警数量**：{incident.alert_count}",
        f"**👤 解决人**：{escape_lark_md(resolver or incident.assignee or '') or '—'}",
    ]
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{escape_lark_md(incident.title[:120])}**"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
    ]

    summary = incident.summary_analysis if isinstance(incident.summary_analysis, dict) else None
    if summary:
        # Model output: escaped like every other untrusted value in a card.
        summary_text = escape_lark_md(str(summary.get("summary") or "").strip()[:400])
        root_cause = escape_lark_md(str(summary.get("root_cause") or "").strip()[:400])
        parts = []
        if summary_text:
            parts.append(f"**📝 事故摘要**\n{summary_text}")
        if root_cause:
            parts.append(f"**🔍 根因**\n{root_cause}")
        if parts:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(parts)}})
    elif incident.summary_status == "pending":
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "📝 AI 事故摘要生成中，稍后可在事故详情与复盘导出中查看。"},
            }
        )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"✅ 事故已解决：{incident.title[:60]}"},
                "template": "green",
            },
            "elements": elements,
        },
    }


async def queue_incident_resolved_recap(session: AsyncSession, incident: Incident, *, resolver: str) -> int | None:
    """Insert one idempotent recap intent when an incident is resolved.

    Same transactional-outbox shape as incident_created; the idempotency key is
    per incident, so a reopen followed by a second resolve does not send a
    second card. Opt-in via INCIDENT_RESOLVE_RECAP_ENABLED (runtime policy).
    """
    from services.operations import runtime_settings as rt

    app_config = get_config_manager()
    cfg = app_config.notifications
    if not rt.override_or("INCIDENT_RESOLVE_RECAP_ENABLED", bool(cfg.INCIDENT_RESOLVE_RECAP_ENABLED)):
        return None
    if incident.id is None:
        return None

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
        "incident_resolved",
        fallback_url=configured,
        fallback_name="incident-resolved-recap",
        fallback_target_type="feishu_app" if app_enabled else "feishu",
    )
    if not target.url and target.target_type != "feishu_app":
        return None

    key = f"incident-resolved-recap:{incident.id}"
    existing = await find_outbox_id_by_key(session, key)
    if existing is not None:
        return existing

    policy = ForwardDeliveryPolicy.from_config()
    now = utcnow()
    record = ForwardOutbox(
        idempotency_key=key,
        webhook_event_id=None,
        original_event_id=None,
        forward_rule_id=target.rule_id,
        rule_name=target.rule_name if target.from_rule else "system:incident-resolved-recap",
        target_type=target.target_type,
        target_url=target.url,
        target_name=target.rule_name,
        channel_name=target.target_type,
        event_type="incident_resolved",
        status=ForwardOutboxStatus.PENDING,
        attempts=0,
        max_attempts=policy.max_attempts,
        next_attempt_at=now,
        formatted_payload=_incident_resolved_card(incident, resolver=resolver),
        created_at=now,
        updated_at=now,
    )
    outbox_id, created = await insert_outbox_or_existing(session, record)
    if created:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(target.target_type, "created").inc()
    return outbox_id


async def queue_sla_breach_notifications(session: AsyncSession, now: Any) -> list[int]:
    """Create idempotent notifications for newly breached alert and incident SLAs.

    This is the escalation path: with the auto-SLA policy armed (see
    services/incidents/auto_sla.py), an unacknowledged incident lands here N
    minutes later. A dedicated escalation webhook and an @all mention can make
    the breach louder than the original alert.
    """
    cfg = get_config_manager().notifications
    configured = str(
        cfg.SLA_BREACH_FEISHU_WEBHOOK or cfg.DEEP_ANALYSIS_FEISHU_WEBHOOK or cfg.WEEKLY_REPORT_FEISHU_WEBHOOK or ""
    ).strip()
    target = await resolve_notification_target(
        "sla_breached", fallback_url=configured, fallback_name="sla-breach-notification"
    )
    target_url = target.url
    if not target_url:
        return []
    from services.operations import runtime_settings as rt

    mention_all = rt.override_or("SLA_BREACH_MENTION_ALL", bool(cfg.SLA_BREACH_MENTION_ALL))

    incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.sla_due_at.isnot(None),
                    Incident.sla_due_at <= now,
                    Incident.workflow_status.notin_(["resolved", "ignored"]),
                    # The escalation exists to find a human; an acknowledged
                    # resource already has one.
                    Incident.acknowledged_at.is_(None),
                )
                .order_by(Incident.sla_due_at, Incident.id)
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    events = list(
        (
            await session.execute(
                select(WebhookEvent)
                .where(
                    WebhookEvent.sla_due_at.isnot(None),
                    WebhookEvent.sla_due_at <= now,
                    WebhookEvent.workflow_status.notin_(["resolved", "ignored"]),
                    WebhookEvent.acknowledged_at.is_(None),
                )
                .order_by(WebhookEvent.sla_due_at, WebhookEvent.id)
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    policy = ForwardDeliveryPolicy.from_config()
    outbox_ids: list[int] = []
    incidents_by_id = {int(item.id): item for item in incidents}
    resources: list[tuple[str, int, str, str, Any, str]] = [
        ("incident", int(item.id), item.title, item.workflow_status, item.sla_due_at, str(item.assignee or ""))
        for item in incidents
    ]
    resources.extend(
        (
            "alert",
            int(item.id),
            str(item.request_id or f"Alert #{item.id}"),
            item.workflow_status,
            item.sla_due_at,
            str(item.assignee or ""),
        )
        for item in events
    )
    # One batched existence check instead of a point-SELECT per breached
    # resource: a breach stays in this result set until resolved, so the scan
    # re-runs every tick and the per-key queries would repeat indefinitely.
    keys_by_resource = [
        (resource, f"sla-breached:{resource[0]}:{resource[1]}:{resource[4].isoformat()}") for resource in resources
    ]
    dashboard_url = str(cfg.DASHBOARD_PUBLIC_URL or "").strip()
    already_queued: set[str] = set()
    if keys_by_resource:
        already_queued = set(
            (
                await session.execute(
                    select(ForwardOutbox.idempotency_key).where(
                        ForwardOutbox.idempotency_key.in_([key for _, key in keys_by_resource])
                    )
                )
            ).scalars()
        )
    for (resource_type, resource_id, title, status, due_at, assignee), key in keys_by_resource:
        if key in already_queued:
            continue
        # Title and assignee are payload/operator text and get escaped; the
        # template's own dashboard link and <at id="all"> below are markup.
        body = (
            f"**Resource:** {resource_type} #{resource_id}\n"
            f"**Title:** {escape_lark_md(title[:160])}\n"
            f"**Workflow status:** {status}\n"
            f"**Assignee:** {escape_lark_md(assignee) or 'unassigned'}\n"
            f"**SLA due:** {due_at.isoformat()}"
        )
        if dashboard_url:
            body += f"\n[Open dashboard]({dashboard_url})"
        if mention_all:
            body += '\n<at id="all"></at> unacknowledged past its SLA — please claim it.'
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "⏰ WebhookWise SLA breached"},
                    "template": "red",
                },
                "elements": [{"tag": "markdown", "content": body}],
            },
        }
        record = ForwardOutbox(
            idempotency_key=key,
            forward_rule_id=target.rule_id,
            rule_name=target.rule_name if target.from_rule else "system:sla-breached",
            target_type=target.target_type,
            target_url=target_url,
            target_name=target.rule_name,
            channel_name=target.target_type,
            event_type="sla_breached",
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=policy.max_attempts,
            next_attempt_at=now,
            formatted_payload=card,
            created_at=now,
            updated_at=now,
        )
        outbox_id, created = await insert_outbox_or_existing(session, record)
        outbox_ids.append(outbox_id)
        if created:
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("feishu", "created").inc()
        # Mark the escalation on the incident itself so it is visible without
        # joining the outbox (dashboard badge, postmortem timeline).
        if resource_type == "incident":
            incident = incidents_by_id.get(resource_id)
            if incident is not None and incident.escalated_at is None:
                incident.escalated_at = now
    return outbox_ids
