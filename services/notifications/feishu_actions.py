"""Signed Feishu incident-card actions and transactional callback handling."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from models import Incident, IntegrationActionReceipt, OperationalNote
from services.incidents.summary import queue_summary_if_needed
from services.operations.audit_logger import add_audit

_ALLOWED_ACTIONS = frozenset({"acknowledge", "resolve", "add_note"})
_CALLBACK_MAX_SKEW_SECONDS = 600
_MAX_NOTE_CHARS = 2_000


class FeishuActionError(ValueError):
    """A verified callback contains an invalid or unauthorized action."""


class FeishuActionConflict(FeishuActionError):
    """An external event id was reused with different content."""


@dataclass(frozen=True, slots=True)
class FeishuActionContext:
    event_id: str
    event_type: str
    tenant_key: str
    operator_open_id: str
    value: dict[str, Any]
    form_value: dict[str, Any]


def _canonical_action(value: dict[str, object]) -> bytes:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    return json.dumps(
        unsigned,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def build_incident_action_value(
    action: str,
    incident_id: int,
    *,
    expires_at: int | None = None,
    secret: str | None = None,
) -> dict[str, object]:
    """Build a short-lived, resource-bound action value for a Feishu card."""
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(f"Unsupported Feishu incident action: {action}")
    config = get_config_manager()
    signing_secret = secret if secret is not None else config.security.FEISHU_CARD_ACTION_SECRET
    if not signing_secret:
        raise ValueError("FEISHU_CARD_ACTION_SECRET is not configured")
    expiry = expires_at or int(time.time()) + int(config.notifications.FEISHU_CARD_ACTION_TTL_SECONDS)
    value: dict[str, object] = {
        "version": 1,
        "action": action,
        "resource_type": "incident",
        "resource_id": int(incident_id),
        "expires_at": int(expiry),
    }
    value["signature"] = hmac.new(signing_secret.encode(), _canonical_action(value), hashlib.sha256).hexdigest()
    return value


def verify_incident_action_value(
    value: dict[str, Any],
    *,
    secret: str | None = None,
    now_epoch: int | None = None,
) -> tuple[str, int]:
    """Verify action allowlist, expiry, resource binding, and HMAC signature."""
    config = get_config_manager()
    signing_secret = secret if secret is not None else config.security.FEISHU_CARD_ACTION_SECRET
    if not signing_secret:
        raise FeishuActionError("Feishu card action signing is not configured")
    action = str(value.get("action") or "")
    resource_type = str(value.get("resource_type") or "")
    if action not in _ALLOWED_ACTIONS or resource_type != "incident":
        raise FeishuActionError("Unsupported Feishu card action")
    try:
        incident_id = int(value["resource_id"])
        expires_at = int(value["expires_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise FeishuActionError("Malformed Feishu card action") from error
    if incident_id <= 0:
        raise FeishuActionError("Malformed Feishu incident id")
    supplied_signature = str(value.get("signature") or "")
    expected_signature = hmac.new(
        signing_secret.encode(),
        _canonical_action(value),
        hashlib.sha256,
    ).hexdigest()
    if not supplied_signature or not hmac.compare_digest(supplied_signature, expected_signature):
        raise FeishuActionError("Invalid Feishu card action signature")
    current_epoch = int(time.time()) if now_epoch is None else int(now_epoch)
    if expires_at < current_epoch:
        raise FeishuActionError("Feishu card action has expired")
    return action, incident_id


def _nested_mapping(value: object, *keys: str) -> dict[str, Any]:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def extract_action_context(payload: dict[str, Any]) -> FeishuActionContext:
    """Normalize current and legacy Feishu card callback envelopes."""
    header = _nested_mapping(payload, "header")
    event = _nested_mapping(payload, "event") or payload
    action_data = _nested_mapping(event, "action")
    value = action_data.get("value")
    if not isinstance(value, dict):
        legacy_action = _nested_mapping(payload, "action")
        action_data = legacy_action or action_data
        value = action_data.get("value")
    if not isinstance(value, dict):
        raise FeishuActionError("Feishu callback has no structured action value")

    operator = _nested_mapping(event, "operator")
    operator_id = _nested_mapping(operator, "operator_id")
    operator_open_id = str(
        operator_id.get("open_id") or operator.get("open_id") or event.get("open_id") or payload.get("open_id") or ""
    ).strip()
    event_id = str(header.get("event_id") or payload.get("event_id") or "").strip()
    if not event_id:
        raise FeishuActionError("Feishu callback has no event id")
    if not operator_open_id:
        raise FeishuActionError("Feishu callback has no operator identity")
    form_value = action_data.get("form_value")
    if not isinstance(form_value, dict):
        form_value = event.get("form_value")
    return FeishuActionContext(
        event_id=event_id,
        event_type=str(header.get("event_type") or payload.get("type") or "").strip(),
        tenant_key=str(header.get("tenant_key") or event.get("tenant_key") or "").strip(),
        operator_open_id=operator_open_id,
        value=value,
        form_value=form_value if isinstance(form_value, dict) else {},
    )


def verify_callback_policy(payload: dict[str, Any], context: FeishuActionContext) -> None:
    """Enforce event type, freshness, tenant, and operator allowlists."""
    config = get_config_manager()
    if context.event_type and context.event_type != "card.action.trigger":
        raise FeishuActionError("Unsupported Feishu callback event type")
    header = _nested_mapping(payload, "header")
    raw_create_time = header.get("create_time")
    if raw_create_time not in (None, ""):
        try:
            create_time = int(str(raw_create_time))
        except ValueError as error:
            raise FeishuActionError("Invalid Feishu callback timestamp") from error
        if create_time > 10_000_000_000:
            create_time //= 1_000
        if abs(int(time.time()) - create_time) > _CALLBACK_MAX_SKEW_SECONDS:
            raise FeishuActionError("Stale Feishu callback")

    allowed_tenants = {item.strip() for item in config.security.FEISHU_ALLOWED_TENANT_KEYS.split(",") if item.strip()}
    if allowed_tenants and context.tenant_key not in allowed_tenants:
        raise FeishuActionError("Feishu tenant is not allowed")
    allowed_operators = {
        item.strip() for item in config.security.FEISHU_ALLOWED_OPERATOR_OPEN_IDS.split(",") if item.strip()
    }
    if allowed_operators and context.operator_open_id not in allowed_operators:
        raise FeishuActionError("Feishu operator is not allowed")


def callback_payload_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def _existing_receipt(
    session: AsyncSession,
    event_id: str,
) -> IntegrationActionReceipt | None:
    return (
        await session.execute(
            select(IntegrationActionReceipt).where(
                IntegrationActionReceipt.provider == "feishu",
                IntegrationActionReceipt.event_id == event_id,
            )
        )
    ).scalar_one_or_none()


def _result_payload(message: str, *, incident: Incident, changed: bool) -> dict[str, object]:
    return {
        "toast": {
            "type": "success",
            "content": message,
        },
        "incident_id": int(incident.id),
        "workflow_status": incident.workflow_status,
        "changed": changed,
    }


async def process_incident_card_action(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    payload_sha256: str,
) -> dict[str, object]:
    """Apply one verified action and persist its idempotency receipt atomically."""
    context = extract_action_context(payload)
    verify_callback_policy(payload, context)
    action, incident_id = verify_incident_action_value(context.value)

    existing = await _existing_receipt(session, context.event_id)
    if existing is not None:
        if not hmac.compare_digest(existing.payload_sha256, payload_sha256):
            raise FeishuActionConflict("Feishu event id was reused with different content")
        return dict(existing.result or {})

    receipt = IntegrationActionReceipt(
        provider="feishu",
        event_id=context.event_id,
        payload_sha256=payload_sha256,
        action=action,
        resource_type="incident",
        resource_id=incident_id,
        actor=context.operator_open_id,
        status="processing",
        result={},
        created_at=utcnow(),
    )
    session.add(receipt)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        existing = await _existing_receipt(session, context.event_id)
        if existing is None:
            raise
        if not hmac.compare_digest(existing.payload_sha256, payload_sha256):
            raise FeishuActionConflict("Feishu event id was reused with different content") from error
        return dict(existing.result or {})

    incident = await session.get(Incident, incident_id)
    if incident is None:
        receipt.status = "rejected"
        receipt.completed_at = utcnow()
        receipt.result = {
            "toast": {"type": "error", "content": f"Incident #{incident_id} was not found"},
            "incident_id": incident_id,
            "changed": False,
        }
        await session.commit()
        return dict(receipt.result)

    now = utcnow()
    changed = False
    message: str
    if action == "acknowledge":
        if incident.workflow_status in {"resolved", "ignored"}:
            message = f"Incident #{incident_id} is already {incident.workflow_status}"
        elif incident.workflow_status in {"acknowledged", "in_progress"}:
            message = f"Incident #{incident_id} is already acknowledged"
        else:
            incident.workflow_status = "acknowledged"
            incident.acknowledged_at = incident.acknowledged_at or now
            changed = True
            message = f"Incident #{incident_id} acknowledged"
    elif action == "resolve":
        if incident.workflow_status == "resolved":
            message = f"Incident #{incident_id} is already resolved"
        else:
            incident.workflow_status = "resolved"
            incident.status = "closed"
            incident.resolved_at = incident.resolved_at or now
            incident.ended_at = incident.ended_at or now
            queue_summary_if_needed(incident, now)
            changed = True
            message = f"Incident #{incident_id} resolved"
    else:
        note_body = str(context.form_value.get("note") or "").strip()
        if not note_body:
            raise FeishuActionError("A note is required")
        if len(note_body) > _MAX_NOTE_CHARS:
            raise FeishuActionError(f"Note exceeds {_MAX_NOTE_CHARS} characters")
        session.add(
            OperationalNote(
                resource_type="incident",
                resource_id=incident_id,
                body=note_body,
                actor=context.operator_open_id,
                created_at=now,
            )
        )
        changed = True
        message = f"Note added to incident #{incident_id}"

    if changed:
        add_audit(
            session,
            "incident",
            incident_id,
            incident.title,
            f"feishu_{action}"[:20],
            f"Feishu card action applied: {action}",
            actor=context.operator_open_id,
        )
    result = _result_payload(message, incident=incident, changed=changed)
    receipt.status = "completed"
    receipt.result = result
    receipt.completed_at = now
    await session.commit()
    return result
