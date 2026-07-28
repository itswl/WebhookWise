"""Managed inbound sources, scoped credentials, and first-event onboarding."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core import json
from core.datetime_utils import utc_isoformat, utcnow
from models.source_connection import SourceConnection
from schemas.onboarding import SourceConnectionCreateRequest, SourceConnectionUpdateRequest
from services.operations.audit_logger import add_audit

_TOKEN_PREFIX = "whsrc_"
_MAX_SHAPE_DEPTH = 4
_MAX_SHAPE_KEYS = 100
_MAX_LIST_SAMPLES = 5

_SOURCE_TEMPLATES: tuple[dict[str, object], ...] = (
    {
        "id": "grafana",
        "name": "Grafana Alerting",
        "description": "Use a webhook contact point with Bearer authorization.",
        "auth_fields": ["scheme=Bearer", "credentials=<source token>"],
        "sample_payload": {
            "receiver": "webhookwise",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "CheckoutErrors",
                        "severity": "warning",
                        "service": "checkout",
                    },
                    "annotations": {"summary": "Checkout error rate is elevated"},
                }
            ],
        },
    },
    {
        "id": "prometheus",
        "name": "Prometheus Alertmanager",
        "description": "Send Alertmanager webhook JSON through the scoped source URL.",
        "auth_fields": ["Authorization: Bearer <source token>"],
        "sample_payload": {
            "version": "4",
            "status": "firing",
            "receiver": "webhookwise",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "HighCPU",
                        "severity": "warning",
                        "service": "api",
                    },
                    "annotations": {"summary": "API CPU is above threshold"},
                }
            ],
        },
    },
    {
        "id": "uptime_kuma",
        "name": "Uptime Kuma",
        "description": "Use a generic webhook notification with an Authorization header.",
        "auth_fields": ["Authorization: Bearer <source token>"],
        "sample_payload": {
            "msg": "[API health] [Down] request timed out",
            "Type": "GenericAlert",
            "Level": "warning",
            "event": "alert",
            "monitor": {
                "id": 1,
                "name": "API health",
                "type": "http",
                "url": "https://api.example.com/health",
            },
            "heartbeat": {"status": 0, "msg": "request timed out"},
        },
    },
    {
        "id": "generic",
        "name": "Generic JSON",
        "description": "Send any JSON object and select or add an adapter later.",
        "auth_fields": ["Authorization: Bearer <source token>"],
        "sample_payload": {
            "event": "alert",
            "severity": "warning",
            "message": "WebhookWise onboarding test",
            "service": "demo-service",
        },
    },
)


class SourceCredentialRevokedError(ValueError):
    """Raised when a revoked credential is re-enabled without rotation."""


def source_templates() -> list[dict[str, object]]:
    """Return stable inbound-source presets for the setup wizard."""
    return [dict(item) for item in _SOURCE_TEMPLATES]


def _new_public_id() -> str:
    return f"src_{secrets.token_urlsafe(12)}"


def _new_token() -> str:
    return f"{_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_hint(token: str) -> str:
    return f"{_TOKEN_PREFIX}…{token[-4:]}"


def source_connection_dict(connection: SourceConnection) -> dict[str, object]:
    """Serialize a connection without exposing its credential digest."""
    credential_state = (
        "revoked" if connection.revoked_at is not None else ("active" if connection.enabled else "disabled")
    )
    return {
        "id": connection.id,
        "public_id": connection.public_id,
        "name": connection.name,
        "source_type": connection.source_type,
        "token_hint": connection.token_hint,
        "enabled": connection.enabled,
        "first_event_at": utc_isoformat(connection.first_event_at),
        "last_event_at": utc_isoformat(connection.last_event_at),
        "last_request_id": connection.last_request_id,
        "event_count": connection.event_count,
        "auth_failure_count": connection.auth_failure_count,
        "last_auth_failure_at": utc_isoformat(connection.last_auth_failure_at),
        "schema_change_count": connection.schema_change_count,
        "schema_changed_at": utc_isoformat(connection.schema_changed_at),
        "created_by": connection.created_by,
        "created_at": utc_isoformat(connection.created_at),
        "updated_at": utc_isoformat(connection.updated_at),
        "rotated_at": utc_isoformat(connection.rotated_at),
        "revoked_at": utc_isoformat(connection.revoked_at),
        "onboarding_status": "connected" if connection.first_event_at else "waiting_for_event",
        "credential_state": credential_state,
    }


def connection_setup(
    connection: SourceConnection,
    webhook_url: str,
    *,
    plaintext_token: str | None = None,
) -> dict[str, object]:
    """Build copyable vendor-neutral setup details.

    ``plaintext_token`` is present only in create/rotate responses.
    """
    credential = plaintext_token or "<rotate-token-to-reveal>"
    return {
        "webhook_url": webhook_url,
        "method": "POST",
        "content_type": "application/json",
        "authorization": {
            "scheme": "Bearer",
            "credentials": credential,
        },
        "headers": {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {credential}",
        },
        "curl": (
            f"curl -X POST {webhook_url} "
            f"-H 'Authorization: Bearer {credential}' "
            "-H 'Content-Type: application/json' "
            '-d \'{"alertname":"WebhookWise onboarding test","severity":"warning"}\''
        ),
        "token_visible_once": plaintext_token is not None,
        "source_type": connection.source_type,
    }


async def create_source_connection(
    session: AsyncSession,
    payload: SourceConnectionCreateRequest,
) -> tuple[SourceConnection, str]:
    token = _new_token()
    connection = SourceConnection(
        public_id=_new_public_id(),
        name=payload.name,
        source_type=payload.source_type.lower(),
        token_hash=_token_digest(token),
        token_hint=_token_hint(token),
        enabled=True,
        created_by=payload.actor,
    )
    session.add(connection)
    await session.flush()
    add_audit(
        session,
        "source_connection",
        connection.id,
        connection.name,
        "created",
        f"Inbound source connection created: {connection.source_type}",
        actor=payload.actor,
    )
    await session.commit()
    await session.refresh(connection)
    return connection, token


async def list_source_connections(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> list[SourceConnection]:
    rows = await session.scalars(
        select(SourceConnection)
        .order_by(SourceConnection.enabled.desc(), SourceConnection.created_at.desc(), SourceConnection.id.desc())
        .limit(max(1, min(int(limit), 200)))
    )
    return list(rows)


async def get_source_connection(session: AsyncSession, connection_id: int) -> SourceConnection | None:
    return await session.get(SourceConnection, connection_id)


async def _lock_source_connection(
    session: AsyncSession,
    connection: SourceConnection,
) -> SourceConnection:
    locked = await session.scalar(
        select(SourceConnection)
        .where(SourceConnection.id == connection.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise ValueError(f"Source connection {connection.id} not found")
    return locked


async def update_source_connection(
    session: AsyncSession,
    connection: SourceConnection,
    payload: SourceConnectionUpdateRequest,
) -> SourceConnection:
    connection = await _lock_source_connection(session, connection)
    if payload.enabled is True and connection.revoked_at is not None:
        raise SourceCredentialRevokedError("A revoked source credential must be rotated before it can be enabled")
    changed: list[str] = []
    if "name" in payload.model_fields_set and payload.name is not None and payload.name != connection.name:
        connection.name = payload.name
        changed.append("name")
    if (
        "source_type" in payload.model_fields_set
        and payload.source_type is not None
        and payload.source_type.lower() != connection.source_type
    ):
        connection.source_type = payload.source_type.lower()
        changed.append("source_type")
    if "enabled" in payload.model_fields_set and payload.enabled is not None and payload.enabled != connection.enabled:
        connection.enabled = payload.enabled
        changed.append("enabled")
    if changed:
        connection.updated_at = utcnow()
        add_audit(
            session,
            "source_connection",
            connection.id,
            connection.name,
            "updated",
            f"Inbound source connection updated: {', '.join(changed)}",
            actor=payload.actor,
        )
        await session.commit()
        await session.refresh(connection)
    return connection


async def rotate_source_token(
    session: AsyncSession,
    connection: SourceConnection,
    *,
    actor: str,
) -> str:
    connection = await _lock_source_connection(session, connection)
    token = _new_token()
    now = utcnow()
    connection.token_hash = _token_digest(token)
    connection.token_hint = _token_hint(token)
    connection.rotated_at = now
    connection.updated_at = now
    connection.enabled = True
    connection.revoked_at = None
    add_audit(
        session,
        "source_connection",
        connection.id,
        connection.name,
        "rotated",
        "Inbound source credential rotated",
        actor=actor,
    )
    await session.commit()
    await session.refresh(connection)
    return token


async def revoke_source_connection(
    session: AsyncSession,
    connection: SourceConnection,
    *,
    actor: str,
) -> SourceConnection:
    connection = await _lock_source_connection(session, connection)
    if connection.enabled or connection.revoked_at is None:
        now = utcnow()
        connection.enabled = False
        connection.revoked_at = now
        connection.updated_at = now
        add_audit(
            session,
            "source_connection",
            connection.id,
            connection.name,
            "revoked",
            "Inbound source credential revoked",
            actor=actor,
        )
        await session.commit()
        await session.refresh(connection)
    return connection


async def source_by_public_id(session: AsyncSession, public_id: str) -> SourceConnection | None:
    return cast(
        SourceConnection | None,
        await session.scalar(select(SourceConnection).where(SourceConnection.public_id == public_id)),
    )


def source_token_matches(connection: SourceConnection, candidates: list[str]) -> bool:
    if not connection.enabled or connection.revoked_at is not None:
        return False
    return any(
        hmac.compare_digest(_token_digest(candidate), connection.token_hash)
        for candidate in candidates
        if candidate.startswith(_TOKEN_PREFIX)
    )


async def record_auth_failure(session: AsyncSession, connection: SourceConnection | None) -> None:
    if connection is None:
        return
    now = utcnow()
    await session.execute(
        update(SourceConnection)
        .where(SourceConnection.id == connection.id)
        .values(
            auth_failure_count=SourceConnection.auth_failure_count + 1,
            last_auth_failure_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    await session.refresh(connection)


def _shape(value: object, *, depth: int = 0) -> object:
    if depth >= _MAX_SHAPE_DEPTH:
        return type(value).__name__
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value)[:_MAX_SHAPE_KEYS]
        return {key: _shape(value.get(key), depth=depth + 1) for key in keys}
    if isinstance(value, list):
        samples = [_shape(item, depth=depth + 1) for item in value[:_MAX_LIST_SAMPLES]]
        unique = sorted({json.dumps(item, sort_keys=True) for item in samples})
        return [json.loads(item) for item in unique]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def payload_schema_fingerprint(raw_body: bytes) -> str | None:
    """Hash JSON structure only; payload values are never retained."""
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    encoded = json.dumps_bytes(_shape(payload), sort_keys=True)
    return hashlib.sha256(encoded).hexdigest()


async def record_source_event(
    session: AsyncSession,
    connection: SourceConnection,
    *,
    request_id: str,
    raw_body: bytes,
    received_at: datetime | None = None,
) -> None:
    now = received_at or utcnow()
    fingerprint = payload_schema_fingerprint(raw_body)
    locked = await session.scalar(
        select(SourceConnection)
        .where(SourceConnection.id == connection.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        return
    schema_changed = bool(
        fingerprint and locked.schema_fingerprint and not hmac.compare_digest(fingerprint, locked.schema_fingerprint)
    )
    values: dict[str, Any] = {
        "first_event_at": func.coalesce(SourceConnection.first_event_at, now),
        "last_event_at": now,
        "last_request_id": request_id,
        "event_count": SourceConnection.event_count + 1,
        "updated_at": now,
    }
    if fingerprint:
        values["schema_fingerprint"] = fingerprint
    if schema_changed:
        values["schema_change_count"] = SourceConnection.schema_change_count + 1
        values["schema_changed_at"] = now
    await session.execute(update(SourceConnection).where(SourceConnection.id == locked.id).values(**values))
    await session.commit()
    await session.refresh(connection)


__all__ = [
    "SourceCredentialRevokedError",
    "connection_setup",
    "create_source_connection",
    "get_source_connection",
    "list_source_connections",
    "payload_schema_fingerprint",
    "record_auth_failure",
    "record_source_event",
    "revoke_source_connection",
    "rotate_source_token",
    "source_by_public_id",
    "source_connection_dict",
    "source_templates",
    "source_token_matches",
    "update_source_connection",
]
