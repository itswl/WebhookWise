from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import AuditLog, Incident, IntegrationActionReceipt, OperationalNote
from services.notifications.feishu_actions import (
    FeishuActionConflict,
    FeishuActionError,
    build_incident_action_value,
    process_incident_card_action,
    verify_incident_action_value,
)


def _callback(
    *,
    event_id: str,
    value: dict[str, object],
    open_id: str = "ou_operator",
    form_value: dict[str, object] | None = None,
) -> dict[str, Any]:
    return {
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "create_time": str(int(time.time() * 1_000)),
            "tenant_key": "tenant-a",
        },
        "event": {
            "operator": {"operator_id": {"open_id": open_id}},
            "action": {
                "value": value,
                "form_value": form_value or {},
            },
        },
    }


def test_feishu_action_value_is_resource_bound_signed_and_expiring() -> None:
    value = build_incident_action_value(
        "acknowledge",
        42,
        expires_at=2_000,
        secret="unit-signing-secret",
    )

    assert verify_incident_action_value(
        value,
        secret="unit-signing-secret",
        now_epoch=1_999,
    ) == ("acknowledge", 42)

    tampered = dict(value)
    tampered["resource_id"] = 43
    with pytest.raises(FeishuActionError, match="signature"):
        verify_incident_action_value(
            tampered,
            secret="unit-signing-secret",
            now_epoch=1_999,
        )
    with pytest.raises(FeishuActionError, match="expired"):
        verify_incident_action_value(
            value,
            secret="unit-signing-secret",
            now_epoch=2_001,
        )


@pytest.mark.asyncio
async def test_feishu_action_is_atomic_idempotent_and_audited(
    db_session: AsyncSession,
    temp_config: Any,
) -> None:
    temp_config.security.FEISHU_CARD_ACTION_SECRET = "unit-signing-secret"
    temp_config.security.FEISHU_ALLOWED_TENANT_KEYS = "tenant-a"
    temp_config.security.FEISHU_ALLOWED_OPERATOR_OPEN_IDS = "ou_operator"
    incident = Incident(
        title="Checkout unavailable",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=utcnow(),
        alert_count=2,
        correlation_dimensions={"service": "checkout"},
    )
    db_session.add(incident)
    await db_session.commit()

    value = build_incident_action_value(
        "acknowledge",
        int(incident.id),
        expires_at=int(time.time()) + 60,
    )
    payload = _callback(event_id="event-1", value=value)

    first = await process_incident_card_action(
        db_session,
        payload,
        payload_sha256="a" * 64,
    )
    second = await process_incident_card_action(
        db_session,
        payload,
        payload_sha256="a" * 64,
    )

    await db_session.refresh(incident)
    assert first == second
    assert first["changed"] is True
    assert incident.workflow_status == "acknowledged"
    assert incident.acknowledged_at is not None
    assert await db_session.scalar(select(func.count(IntegrationActionReceipt.id))) == 1
    assert await db_session.scalar(select(func.count(AuditLog.id))) == 1

    with pytest.raises(FeishuActionConflict):
        await process_incident_card_action(
            db_session,
            payload,
            payload_sha256="b" * 64,
        )


@pytest.mark.asyncio
async def test_acknowledge_from_card_assigns_the_operator(
    db_session: AsyncSession,
    temp_config: Any,
) -> None:
    """The confirm dialog has always said "this assigns it to you"; the handler
    now keeps that promise — without ever overwriting an existing assignee."""
    temp_config.security.FEISHU_CARD_ACTION_SECRET = "unit-signing-secret"
    temp_config.security.FEISHU_ALLOWED_TENANT_KEYS = "tenant-a"
    temp_config.security.FEISHU_ALLOWED_OPERATOR_OPEN_IDS = "ou_operator"
    unowned = Incident(
        title="a", status="active", workflow_status="open", source="grafana", started_at=utcnow(), alert_count=1
    )
    owned = Incident(
        title="b",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=utcnow(),
        alert_count=1,
        assignee="adrian",
    )
    db_session.add_all([unowned, owned])
    await db_session.commit()

    for event, incident in (("ack-1", unowned), ("ack-2", owned)):
        value = build_incident_action_value("acknowledge", int(incident.id), expires_at=int(time.time()) + 60)
        await process_incident_card_action(db_session, _callback(event_id=event, value=value), payload_sha256="c" * 64)
    await db_session.refresh(unowned)
    await db_session.refresh(owned)
    assert unowned.assignee == "ou_operator"
    assert owned.assignee == "adrian", "acknowledging someone else's incident is support, not theft"


@pytest.mark.asyncio
async def test_quick_silence_from_card_is_scoped_and_expiring(
    db_session: AsyncSession,
    temp_config: Any,
) -> None:
    """Silence-2h creates a bounded silence matching the incident's identity —
    and refuses outright when the incident carries no match dimension, because
    an all-empty silence would mute the entire ingress from one card tap."""
    from models import Silence

    temp_config.security.FEISHU_CARD_ACTION_SECRET = "unit-signing-secret"
    temp_config.security.FEISHU_ALLOWED_TENANT_KEYS = "tenant-a"
    temp_config.security.FEISHU_ALLOWED_OPERATOR_OPEN_IDS = "ou_operator"
    scoped = Incident(
        title="checkout 5xx",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=utcnow(),
        alert_count=3,
        correlation_dimensions={"service": "checkout", "project": "shop", "environment": "prod"},
    )
    dimensionless = Incident(
        title="mystery", status="active", workflow_status="open", source="", started_at=utcnow(), alert_count=1
    )
    db_session.add_all([scoped, dimensionless])
    await db_session.commit()

    value = build_incident_action_value("silence_2h", int(scoped.id), expires_at=int(time.time()) + 60)
    result = await process_incident_card_action(
        db_session, _callback(event_id="sil-1", value=value), payload_sha256="d" * 64
    )
    assert result["changed"] is True

    silence = (await db_session.execute(select(Silence).order_by(Silence.id.desc()))).scalars().first()
    assert silence is not None
    assert silence.match_source == "grafana"
    assert silence.match_project == "shop"
    assert silence.match_environment == "prod"
    assert silence.created_by == "ou_operator"
    assert silence.expires_at is not None

    value = build_incident_action_value("silence_2h", int(dimensionless.id), expires_at=int(time.time()) + 60)
    refused = await process_incident_card_action(
        db_session, _callback(event_id="sil-2", value=value), payload_sha256="e" * 64
    )
    assert refused["changed"] is False
    assert "no match dimensions" in str(refused)


@pytest.mark.asyncio
async def test_feishu_note_uses_verified_operator_identity(
    db_session: AsyncSession,
    temp_config: Any,
) -> None:
    temp_config.security.FEISHU_CARD_ACTION_SECRET = "unit-signing-secret"
    incident = Incident(
        title="Queue lag",
        status="active",
        workflow_status="open",
        source="prometheus",
        started_at=utcnow(),
        alert_count=3,
        correlation_dimensions={"service": "worker"},
    )
    db_session.add(incident)
    await db_session.commit()
    value = build_incident_action_value(
        "add_note",
        int(incident.id),
        expires_at=int(time.time()) + 60,
    )

    result = await process_incident_card_action(
        db_session,
        _callback(
            event_id="event-note",
            value=value,
            form_value={"note": "Rollback started"},
        ),
        payload_sha256="c" * 64,
    )

    note = (await db_session.execute(select(OperationalNote))).scalar_one()
    assert result["changed"] is True
    assert note.body == "Rollback started"
    assert note.actor == "ou_operator"


@pytest.mark.asyncio
async def test_incident_notification_uses_app_channel_only_when_fully_configured(
    db_session: AsyncSession,
    temp_config: Any,
) -> None:
    from models import ForwardOutbox
    from services.forwarding.channels import resolve_channel
    from services.incidents.notifications import queue_incident_notifications

    temp_config.notifications.FEISHU_CARD_ACTIONS_ENABLED = True
    temp_config.notifications.FEISHU_APP_ID = "cli_app"
    temp_config.notifications.FEISHU_APP_SECRET = "app-secret-value"
    temp_config.notifications.FEISHU_INCIDENT_CHAT_ID = "oc_incidents"
    temp_config.notifications.DASHBOARD_PUBLIC_URL = "https://webhookwise.example/"
    temp_config.security.FEISHU_CARD_VERIFICATION_TOKEN = "verification-token"
    temp_config.security.FEISHU_CARD_ACTION_SECRET = "unit-signing-secret"
    incident = Incident(
        title="API unavailable",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=utcnow(),
        alert_count=4,
        correlation_dimensions={"service": "api"},
    )
    db_session.add(incident)
    await db_session.flush()

    outbox_ids = await queue_incident_notifications(db_session, [incident])
    await db_session.commit()

    outbox = await db_session.get(ForwardOutbox, outbox_ids[0])
    assert outbox is not None
    assert outbox.target_type == "feishu_app"
    assert outbox.target_url == "feishu-app://oc_incidents"
    assert resolve_channel(outbox).name == "feishu_app"
    card = outbox.formatted_payload["card"]
    assert card["config"] == {"enable_forward": False, "update_multi": True}
    rendered = str(card)
    assert "Acknowledge" in rendered
    assert "Resolve" in rendered
    assert "Add note" in rendered
    assert "unit-signing-secret" not in rendered


@pytest.mark.asyncio
async def test_feishu_app_transport_uses_fixed_api_and_interactive_content(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from services.notifications import feishu_app_transport

    temp_config.notifications.FEISHU_APP_ID = "cli_app"
    temp_config.notifications.FEISHU_APP_SECRET = "app-secret"
    temp_config.notifications.FEISHU_WEBHOOK_TIMEOUT_SECONDS = 7
    feishu_app_transport._cached_app_id = ""
    feishu_app_transport._cached_token = ""
    feishu_app_transport._cached_until = 0

    token_response = MagicMock()
    token_response.status_code = 200
    token_response.raise_for_status = MagicMock()
    token_response.json.return_value = {
        "code": 0,
        "tenant_access_token": "tenant-token",
        "expire": 7200,
    }
    message_response = MagicMock()
    message_response.status_code = 200
    message_response.raise_for_status = MagicMock()
    message_response.json.return_value = {"code": 0}
    client = AsyncMock()
    client.post.side_effect = [token_response, message_response]

    class _Breaker:
        async def call_async(self, call):
            return await call()

    monkeypatch.setattr(feishu_app_transport, "feishu_cb", _Breaker())

    result = await feishu_app_transport.send_to_feishu_app(
        "oc_chat",
        {
            "msg_type": "interactive",
            "card": {"elements": [{"tag": "markdown", "content": "hello"}]},
        },
        idempotency_key="incident-created:7",
        http_client=client,
    )

    assert result == {"status": "success", "status_code": 200}
    assert client.post.await_count == 2
    token_call, message_call = client.post.await_args_list
    assert token_call.args[0] == ("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal")
    assert message_call.args[0] == "https://open.feishu.cn/open-apis/im/v1/messages"
    assert message_call.kwargs["params"] == {"receive_id_type": "chat_id"}
    assert message_call.kwargs["headers"]["Authorization"] == "Bearer tenant-token"
    # Feishu dedupes on the BODY uuid field (<=50 chars), not any header;
    # the outbox key is hashed to fit deterministically.
    assert "Idempotency-Key" not in message_call.kwargs["headers"]
    assert message_call.kwargs["json"]["uuid"] == "07c8b8dde16e81b7df49962a9ee1d2e499bb11b1"
    assert len(message_call.kwargs["json"]["uuid"]) <= 50
    assert message_call.kwargs["json"]["receive_id"] == "oc_chat"
    assert '"elements"' in message_call.kwargs["json"]["content"]
