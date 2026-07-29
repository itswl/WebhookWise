from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.sensitive_data import REDACTED, redact_headers
from models import SourceConnection
from schemas.onboarding import SourceConnectionCreateRequest, SourceConnectionUpdateRequest
from services.webhooks.source_onboarding import (
    SourceCredentialRevokedError,
    create_source_connection,
    payload_schema_fingerprint,
    record_auth_failure,
    record_source_event,
    revoke_source_connection,
    rotate_source_token,
    source_connection_dict,
    source_token_matches,
    update_source_connection,
)


async def test_source_credential_is_returned_once_and_only_digest_is_persisted(
    db_session: AsyncSession,
) -> None:
    connection, token = await create_source_connection(
        db_session,
        SourceConnectionCreateRequest(
            name="Production Grafana",
            source_type="grafana",
            actor="alice",
        ),
    )

    assert token.startswith("whsrc_")
    assert connection.token_hash != token
    assert token not in connection.token_hash
    assert connection.token_hint.endswith(token[-4:])
    assert source_token_matches(connection, [token])
    assert not source_token_matches(connection, ["whsrc_wrong"])

    serialized = source_connection_dict(connection)
    assert "token_hash" not in serialized
    assert "token" not in serialized
    assert serialized["onboarding_status"] == "waiting_for_event"

    persisted = await db_session.get(SourceConnection, connection.id)
    assert persisted is not None
    assert token not in repr(persisted.__dict__)
    assert redact_headers({"X-Source-Token": token}) == {"X-Source-Token": REDACTED}


async def test_source_credential_rotation_and_revocation_are_immediate(
    db_session: AsyncSession,
) -> None:
    connection, old_token = await create_source_connection(
        db_session,
        SourceConnectionCreateRequest(name="Alertmanager", source_type="prometheus"),
    )

    new_token = await rotate_source_token(db_session, connection, actor="bob")
    assert new_token != old_token
    assert not source_token_matches(connection, [old_token])
    assert source_token_matches(connection, [new_token])

    await revoke_source_connection(db_session, connection, actor="bob")
    assert connection.enabled is False
    assert connection.revoked_at is not None
    assert source_connection_dict(connection)["credential_state"] == "revoked"
    assert not source_token_matches(connection, [new_token])
    with pytest.raises(SourceCredentialRevokedError):
        await update_source_connection(
            db_session,
            connection,
            SourceConnectionUpdateRequest(enabled=True),
        )


async def test_source_updates_and_auth_failures_are_tracked(
    db_session: AsyncSession,
) -> None:
    connection, _ = await create_source_connection(
        db_session,
        SourceConnectionCreateRequest(name="Old name", source_type="generic"),
    )
    updated = await update_source_connection(
        db_session,
        connection,
        SourceConnectionUpdateRequest(
            name="Sentry production",
            source_type="sentry",
            actor="carol",
        ),
    )
    await record_auth_failure(db_session, updated)

    assert updated.name == "Sentry production"
    assert updated.source_type == "sentry"
    assert updated.auth_failure_count == 1
    assert updated.last_auth_failure_at is not None


async def test_source_schema_tracking_ignores_values_and_detects_shape_changes(
    db_session: AsyncSession,
) -> None:
    first = b'{"alerts":[{"labels":{"service":"checkout"},"status":"firing"}],"count":1}'
    same_shape = b'{"count":99,"alerts":[{"status":"resolved","labels":{"service":"billing"}}]}'
    changed_shape = (
        b'{"alerts":[{"labels":{"service":"checkout","region":"cn"},"status":"firing"}],"count":1,"receiver":"ops"}'
    )
    assert payload_schema_fingerprint(first) == payload_schema_fingerprint(same_shape)
    assert payload_schema_fingerprint(first) != payload_schema_fingerprint(changed_shape)
    assert payload_schema_fingerprint(b"not-json") is None

    connection, _ = await create_source_connection(
        db_session,
        SourceConnectionCreateRequest(name="Grafana", source_type="grafana"),
    )
    await record_source_event(
        db_session,
        connection,
        request_id="request-1",
        raw_body=first,
    )
    await record_source_event(
        db_session,
        connection,
        request_id="request-2",
        raw_body=same_shape,
    )
    await record_source_event(
        db_session,
        connection,
        request_id="request-3",
        raw_body=changed_shape,
    )

    assert connection.first_event_at is not None
    assert connection.last_event_at is not None
    assert connection.last_request_id == "request-3"
    assert connection.event_count == 3
    assert connection.schema_change_count == 1
    assert connection.schema_changed_at is not None
    assert source_connection_dict(connection)["onboarding_status"] == "connected"


@pytest.mark.real_httpx
async def test_management_api_reveals_source_token_only_on_create(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.app import app
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    assert context is not None
    app.state.app_context = context
    monkeypatch.setattr(context.config.security, "API_KEY", "read-token")
    monkeypatch.setattr(context.config.security, "ADMIN_WRITE_KEY", "write-token")
    monkeypatch.setattr(context.config.security, "ADMIN_API_RATE_LIMIT_PER_MINUTE", 0)

    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/onboarding/sources",
            headers={
                "Authorization": "Bearer write-token",
                "X-API-Key": "read-token",
            },
            json={
                "name": "Production Grafana",
                "source_type": "grafana",
                "actor": "alice",
            },
        )
        assert created.status_code == 201
        created_data = created.json()["data"]
        token = created_data["setup"]["authorization"]["credentials"]
        connection_id = created_data["connection"]["id"]
        assert token.startswith("whsrc_")

        loaded = await client.get(
            f"/v1/onboarding/sources/{connection_id}",
            headers={"Authorization": "Bearer read-token"},
        )

    assert loaded.status_code == 200
    loaded_data = loaded.json()["data"]
    assert loaded_data["setup"]["authorization"]["credentials"] == "<rotate-token-to-reveal>"
    assert token not in loaded.text
    assert "token_hash" not in loaded.text


@pytest.mark.real_httpx
async def test_scoped_ingress_authenticates_and_forces_configured_source(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.app import app
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    assert context is not None
    app.state.app_context = context
    monkeypatch.setattr(context.config.security, "WEBHOOK_RATE_LIMIT_PER_MINUTE", 0)

    async with db_app_context_session_factory() as session:
        connection, token = await create_source_connection(
            session,
            SourceConnectionCreateRequest(name="Managed Grafana", source_type="grafana"),
        )
        public_id = connection.public_id
        connection_id = connection.id

    received: list[dict[str, Any]] = []

    async def fake_receive(request: Any, source: str | None = None) -> dict[str, Any]:
        received.append(
            {
                "source": source,
                "body": await request.body(),
                "authorization": request.headers.get("authorization"),
                "source_connection_id": request.state.source_connection_id,
                "source_scope": request.state.webhook_source_scope,
            }
        )
        return {
            "success": True,
            "message": "Webhook received and queued for processing",
            "outcome": "queued",
            "event_id": None,
            "request_id": "managed-request-1",
        }

    monkeypatch.setattr("api.v1.onboarding.receive_webhook", fake_receive)
    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        rejected = await client.post(
            f"/v1/source-webhooks/{public_id}",
            json={"receiver": "ops"},
            headers={"Authorization": "Bearer whsrc_wrong"},
        )
        accepted = await client.post(
            f"/v1/source-webhooks/{public_id}",
            json={"receiver": "ops"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Webhook-Source": "spoofed",
            },
        )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert received == [
        {
            "source": "grafana",
            "body": b'{"receiver":"ops"}',
            "authorization": f"Bearer {token}",
            "source_connection_id": connection_id,
            "source_scope": public_id,
        }
    ]

    async with db_app_context_session_factory() as session:
        persisted = await session.get(SourceConnection, connection_id)
        assert persisted is not None
        assert persisted.auth_failure_count == 1
        assert persisted.event_count == 1
        assert persisted.last_request_id == "managed-request-1"
        assert persisted.first_event_at is not None


@pytest.mark.real_httpx
async def test_scoped_ingress_does_not_retry_queued_alert_when_status_tracking_fails(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.app import app
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    assert context is not None
    app.state.app_context = context
    monkeypatch.setattr(context.config.security, "WEBHOOK_RATE_LIMIT_PER_MINUTE", 0)

    async with db_app_context_session_factory() as session:
        connection, token = await create_source_connection(
            session,
            SourceConnectionCreateRequest(name="Managed Grafana", source_type="grafana"),
        )
        public_id = connection.public_id

    async def fake_receive(request: Any, source: str | None = None) -> dict[str, Any]:
        return {
            "success": True,
            "message": "Webhook received and queued for processing",
            "outcome": "queued",
            "event_id": None,
            "request_id": "managed-request-2",
        }

    async def fail_status_tracking(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("status tracking unavailable")

    monkeypatch.setattr("api.v1.onboarding.receive_webhook", fake_receive)
    monkeypatch.setattr("api.v1.onboarding.record_source_event", fail_status_tracking)
    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/source-webhooks/{public_id}",
            json={"receiver": "ops"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["request_id"] == "managed-request-2"


@pytest.mark.real_httpx
async def test_invalid_source_credential_still_returns_unauthorized_when_audit_tracking_fails(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.app import app
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    assert context is not None
    app.state.app_context = context
    monkeypatch.setattr(context.config.security, "WEBHOOK_RATE_LIMIT_PER_MINUTE", 0)

    async with db_app_context_session_factory() as session:
        connection, _ = await create_source_connection(
            session,
            SourceConnectionCreateRequest(name="Managed Grafana", source_type="grafana"),
        )
        public_id = connection.public_id

    async def fail_auth_tracking(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("authentication telemetry unavailable")

    monkeypatch.setattr("api.v1.onboarding.record_auth_failure", fail_auth_tracking)
    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/source-webhooks/{public_id}",
            json={"receiver": "ops"},
            headers={"Authorization": "Bearer whsrc_wrong"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or revoked source credential"


async def test_managed_event_persists_its_source_connection(
    db_session: AsyncSession,
) -> None:
    from models import WebhookEvent
    from services.webhooks.command_service import (
        SaveWebhookInput,
        save_webhook_data_in_session,
    )

    connection, _ = await create_source_connection(
        db_session,
        SourceConnectionCreateRequest(name="Production Grafana", source_type="grafana"),
    )
    saved = await save_webhook_data_in_session(
        db_session,
        input=SaveWebhookInput(
            data={"message": "checkout failed"},
            source="grafana",
            source_connection_id=int(connection.id),
            request_id="scoped-persistence-request",
            alert_hash="a" * 64,
            dedup_key="b" * 64,
            ai_analysis={"importance": "medium", "summary": "Checkout failed"},
            skip_duplicate_lookup=True,
        ),
    )
    await db_session.commit()

    event = await db_session.get(WebhookEvent, saved.webhook_id)
    assert event is not None
    assert event.source == "grafana"
    assert event.source_connection_id == connection.id


async def test_managed_save_fallback_hash_is_connection_scoped(
    db_session: AsyncSession,
) -> None:
    from models import WebhookEvent
    from services.webhooks.command_service import (
        SaveWebhookInput,
        save_webhook_data_in_session,
    )

    first, _ = await create_source_connection(
        db_session,
        SourceConnectionCreateRequest(name="Grafana A", source_type="grafana"),
    )
    second, _ = await create_source_connection(
        db_session,
        SourceConnectionCreateRequest(name="Grafana B", source_type="grafana"),
    )
    payload = {"alertname": "CheckoutErrors", "service": "checkout"}
    first_result = await save_webhook_data_in_session(
        db_session,
        input=SaveWebhookInput(
            data=payload,
            source="grafana",
            source_connection_id=int(first.id),
            request_id="managed-fallback-hash-a",
            ai_analysis={"importance": "medium", "summary": "Checkout errors"},
        ),
    )
    second_result = await save_webhook_data_in_session(
        db_session,
        input=SaveWebhookInput(
            data=payload,
            source="grafana",
            source_connection_id=int(second.id),
            request_id="managed-fallback-hash-b",
            ai_analysis={"importance": "medium", "summary": "Checkout errors"},
        ),
    )
    await db_session.commit()

    first_event = await db_session.get(WebhookEvent, first_result.webhook_id)
    second_event = await db_session.get(WebhookEvent, second_result.webhook_id)
    assert first_event is not None and second_event is not None
    assert first_event.alert_hash != second_event.alert_hash
    assert first_result.is_duplicate is False
    assert second_result.is_duplicate is False


@pytest.mark.real_httpx
async def test_two_same_type_sources_isolate_request_ids_and_queued_scope(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from api.app import app
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    assert context is not None
    app.state.app_context = context
    monkeypatch.setattr(context.config.security, "WEBHOOK_RATE_LIMIT_PER_MINUTE", 0)

    async with db_app_context_session_factory() as session:
        first, first_token = await create_source_connection(
            session,
            SourceConnectionCreateRequest(name="Grafana A", source_type="grafana"),
        )
        second, second_token = await create_source_connection(
            session,
            SourceConnectionCreateRequest(name="Grafana B", source_type="grafana"),
        )
        first_public_id, second_public_id = first.public_id, second.public_id
        first_id, second_id = int(first.id), int(second.id)

    queued: list[dict[str, Any]] = []

    async def allow_ingress(**kwargs: Any) -> Any:
        return SimpleNamespace(suppressed=False)

    async def allow_queue(**kwargs: Any) -> Any:
        return SimpleNamespace(reject=False)

    async def capture_task(**kwargs: Any) -> None:
        queued.append(kwargs)

    monkeypatch.setattr("api.v1.webhook.check_ingress_backpressure", allow_ingress)
    monkeypatch.setattr("api.v1.webhook.check_queue_backpressure", allow_queue)
    monkeypatch.setattr("api.v1.webhook.process_webhook_task.kiq", capture_task)

    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first_response = await client.post(
            f"/v1/source-webhooks/{first_public_id}",
            json={"receiver": "ops", "status": "firing"},
            headers={
                "Authorization": f"Bearer {first_token}",
                "X-Request-ID": "same-upstream-request",
            },
        )
        second_response = await client.post(
            f"/v1/source-webhooks/{second_public_id}",
            json={"receiver": "ops", "status": "firing"},
            headers={
                "Authorization": f"Bearer {second_token}",
                "X-Request-ID": "same-upstream-request",
            },
        )

    assert first_response.status_code == second_response.status_code == 200
    assert first_response.json()["request_id"] != second_response.json()["request_id"]
    assert [item["source_name"] for item in queued] == ["grafana", "grafana"]
    assert [item["source_connection_id"] for item in queued] == [first_id, second_id]
