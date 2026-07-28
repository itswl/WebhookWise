from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core.datetime_utils import utcnow


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_response_center_api_success_and_validation_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import response_center

    session = AsyncMock()
    queue_result = {
        "items": [{"incident_id": 7}],
        "counts": {"active": 1},
        "offset": 0,
        "next_offset": None,
        "total_matches": 1,
        "has_more": False,
    }
    gaps_result = {"items": [{"pattern": "checkout timeout"}], "summary": {"incident_scan_count": 3}}
    queue = AsyncMock(return_value=queue_result)
    gaps = AsyncMock(return_value=gaps_result)
    monkeypatch.setattr(response_center, "get_response_work_queue", queue)
    monkeypatch.setattr(response_center, "get_knowledge_gaps", gaps)

    missing_actor = await response_center.get_work_queue_endpoint(
        bucket="my",
        actor=" ",
        limit=10,
        offset=0,
        sla_risk_minutes=120,
        session=session,
    )
    assert missing_actor.status_code == 422

    work_queue = await response_center.get_work_queue_endpoint(
        bucket="active",
        actor="alice",
        limit=10,
        offset=0,
        sla_risk_minutes=60,
        session=session,
    )
    knowledge_gaps = await response_center.get_knowledge_gaps_endpoint(
        window_days=30,
        limit=20,
        session=session,
    )

    assert _body(work_queue)["data"] == queue_result
    assert _body(knowledge_gaps)["data"] == gaps_result
    queue.assert_awaited_once_with(
        session,
        bucket="active",
        actor="alice",
        limit=10,
        offset=0,
        sla_risk_minutes=60,
    )
    gaps.assert_awaited_once_with(session, window_days=30, limit=20)


@pytest.mark.asyncio
async def test_response_center_api_sanitizes_service_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import response_center

    session = AsyncMock()
    monkeypatch.setattr(
        response_center,
        "get_response_work_queue",
        AsyncMock(side_effect=ValueError("unsupported bucket")),
    )
    invalid = await response_center.get_work_queue_endpoint(
        bucket="active",
        actor="",
        limit=10,
        offset=0,
        sla_risk_minutes=60,
        session=session,
    )
    assert invalid.status_code == 422
    assert _body(invalid)["error"] == "unsupported bucket"

    monkeypatch.setattr(
        response_center,
        "get_response_work_queue",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    failed_queue = await response_center.get_work_queue_endpoint(
        bucket="active",
        actor="",
        limit=10,
        offset=0,
        sla_risk_minutes=60,
        session=session,
    )
    monkeypatch.setattr(
        response_center,
        "get_knowledge_gaps",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    failed_gaps = await response_center.get_knowledge_gaps_endpoint(
        window_days=30,
        limit=20,
        session=session,
    )

    assert failed_queue.status_code == 500
    assert failed_gaps.status_code == 500
    assert _body(failed_queue)["error"] == "Internal server error"


@pytest.mark.asyncio
async def test_alert_quality_api_is_read_only_and_sanitizes_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import alert_quality

    session = AsyncMock()
    overview = {"read_only": True, "sources": [], "summary": {"quality_score": None}}
    build = AsyncMock(return_value=overview)
    monkeypatch.setattr(alert_quality, "get_alert_quality_overview", build)

    response = await alert_quality.get_alert_quality_endpoint(
        window_days=30,
        source_limit=50,
        session=session,
    )
    assert response.status_code == 200
    assert _body(response)["data"] == overview
    build.assert_awaited_once_with(session, window_days=30, source_limit=50)

    monkeypatch.setattr(
        alert_quality,
        "get_alert_quality_overview",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    failed = await alert_quality.get_alert_quality_endpoint(
        window_days=7,
        source_limit=100,
        session=session,
    )
    assert failed.status_code == 500
    assert _body(failed)["error"] == "Internal server error"


def _source_connection() -> Any:
    from models import SourceConnection

    now = utcnow()
    return SourceConnection(
        id=7,
        public_id="src_abcdefgh",
        name="Production Grafana",
        source_type="grafana",
        token_hash="a" * 64,
        token_hint="whsrc_…1234",
        enabled=True,
        event_count=0,
        auth_failure_count=0,
        schema_change_count=0,
        created_by="alice",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_onboarding_management_api_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import onboarding
    from schemas.onboarding import (
        SourceConnectionActionRequest,
        SourceConnectionCreateRequest,
        SourceConnectionUpdateRequest,
    )

    connection = _source_connection()
    request = SimpleNamespace(base_url="https://hooks.example/")
    session = AsyncMock()
    monkeypatch.setattr(onboarding, "source_templates", lambda: [{"id": "grafana"}])
    monkeypatch.setattr(onboarding, "list_source_connections", AsyncMock(return_value=[connection]))
    monkeypatch.setattr(
        onboarding,
        "create_source_connection",
        AsyncMock(return_value=(connection, "whsrc_plaintext")),
    )
    monkeypatch.setattr(onboarding, "get_source_connection", AsyncMock(return_value=connection))
    monkeypatch.setattr(onboarding, "update_source_connection", AsyncMock(return_value=connection))
    monkeypatch.setattr(onboarding, "rotate_source_token", AsyncMock(return_value="whsrc_rotated"))
    monkeypatch.setattr(onboarding, "revoke_source_connection", AsyncMock(return_value=connection))

    source_types = await onboarding.list_source_types_endpoint()
    listed = await onboarding.list_sources_endpoint(request, limit=25, session=session)
    created = await onboarding.create_source_endpoint(
        SourceConnectionCreateRequest(name="Production Grafana", source_type="grafana", actor="alice"),
        request,
        session=session,
    )
    loaded = await onboarding.get_source_endpoint(7, request, session=session)
    updated = await onboarding.update_source_endpoint(
        7,
        SourceConnectionUpdateRequest(name="Renamed Grafana", actor="alice"),
        request,
        session=session,
    )
    status = await onboarding.source_status_endpoint(7, request, session=session)
    rotated = await onboarding.rotate_source_endpoint(
        7,
        SourceConnectionActionRequest(actor="alice"),
        request,
        session=session,
    )
    revoked = await onboarding.revoke_source_endpoint(
        7,
        SourceConnectionActionRequest(actor="alice"),
        session=session,
    )

    assert _body(source_types)["data"] == [{"id": "grafana"}]
    assert _body(listed)["data"][0]["webhook_url"] == "https://hooks.example/v1/source-webhooks/src_abcdefgh"
    assert created.status_code == 201
    assert _body(created)["data"]["setup"]["authorization"]["credentials"] == "whsrc_plaintext"
    assert _body(loaded)["data"]["setup"]["authorization"]["credentials"] == "<rotate-token-to-reveal>"
    assert _body(updated)["success"] is True
    assert _body(status)["data"]["credential_state"] == "active"
    assert _body(rotated)["data"]["setup"]["authorization"]["credentials"] == "whsrc_rotated"
    assert _body(revoked)["data"]["id"] == 7


@pytest.mark.asyncio
async def test_onboarding_management_api_not_found_and_failure_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import onboarding
    from schemas.onboarding import (
        SourceConnectionActionRequest,
        SourceConnectionCreateRequest,
        SourceConnectionUpdateRequest,
    )

    request = SimpleNamespace(base_url="https://hooks.example/")
    session = AsyncMock()
    monkeypatch.setattr(onboarding, "get_source_connection", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as missing:
        await onboarding.get_source_endpoint(404, request, session=session)
    assert missing.value.status_code == 404

    monkeypatch.setattr(
        onboarding,
        "list_source_connections",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    listed = await onboarding.list_sources_endpoint(request, limit=25, session=session)

    monkeypatch.setattr(
        onboarding,
        "create_source_connection",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    created = await onboarding.create_source_endpoint(
        SourceConnectionCreateRequest(name="Grafana", source_type="grafana"),
        request,
        session=session,
    )

    connection = _source_connection()
    monkeypatch.setattr(onboarding, "get_source_connection", AsyncMock(return_value=connection))
    monkeypatch.setattr(
        onboarding,
        "update_source_connection",
        AsyncMock(side_effect=onboarding.SourceCredentialRevokedError("rotate before enabling")),
    )
    with pytest.raises(HTTPException) as conflict:
        await onboarding.update_source_endpoint(
            7,
            SourceConnectionUpdateRequest(enabled=True),
            request,
            session=session,
        )
    assert conflict.value.status_code == 409

    monkeypatch.setattr(
        onboarding,
        "rotate_source_token",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    rotated = await onboarding.rotate_source_endpoint(
        7,
        SourceConnectionActionRequest(),
        request,
        session=session,
    )
    monkeypatch.setattr(
        onboarding,
        "revoke_source_connection",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    revoked = await onboarding.revoke_source_endpoint(
        7,
        SourceConnectionActionRequest(),
        session=session,
    )

    assert listed.status_code == 500
    assert created.status_code == 500
    assert rotated.status_code == 500
    assert revoked.status_code == 500
    assert session.rollback.await_count == 4
