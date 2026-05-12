from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_: object, compiler: object, **kw: object) -> str:
    return "JSON"


@pytest.fixture()
async def integration_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import db.session as db_session
    import models  # noqa: F401 - register all SQLAlchemy models
    from db.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(db_session, "_session_factory", session_factory)

    yield session_factory

    await engine.dispose()


async def test_webhook_receive_to_feishu_card_flow(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setattr("core.otel._enabled_cache", None)

    from core.app import app
    from core.config import Config, get_settings
    from models import WebhookEvent
    from services.operations.tasks import process_webhook_task
    from services.webhooks.pipeline import handle_webhook_ingest, handle_webhook_process

    monkeypatch.setattr(Config, "_overrides", dict(Config._overrides))
    monkeypatch.setattr(Config, "_meta", dict(Config._meta))
    settings = get_settings()
    monkeypatch.setattr(settings.security, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings.security, "API_KEY", "")
    monkeypatch.setattr(settings.security, "WEBHOOK_RATE_LIMIT_PER_MINUTE", 0)
    Config.set_override("FORWARD_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/test-token", source="test")
    Config.set_override("ENABLE_ALERT_NOISE_REDUCTION", False, source="test")

    async def fake_analyze_webhook_with_ai(webhook_data: dict[str, Any], **_: object) -> dict[str, Any]:
        parsed = webhook_data["parsed_data"]
        return {
            "importance": "high",
            "summary": f"订单服务错误率升高: {parsed['alert_name']}",
            "impact_scope": "checkout-api",
            "actions": ["检查 5xx 日志", "回滚最近发布"],
            "event_type": "integration_test_alert",
        }

    monkeypatch.setattr("services.webhooks.analysis_resolution.analyze_webhook_with_ai", fake_analyze_webhook_with_ai)

    posted: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self) -> dict[str, Any]:
            return {}

        def raise_for_status(self) -> None:
            return None

    class FakeHttpClient:
        async def post(self, url: str, *, json: dict[str, Any], timeout: int) -> FakeResponse:
            posted.append({"url": url, "json": json, "timeout": timeout})
            return FakeResponse()

    monkeypatch.setattr("services.forwarding.forward.get_http_client", lambda: FakeHttpClient())

    async def accept_url(url: str) -> str:
        return url

    monkeypatch.setattr("services.forwarding.forward.validate_outbound_url", accept_url)

    async def run_task_inline(
        event_id: int | None = None,
        client_ip: str | None = None,
        source: str | None = None,
        raw_headers: dict[str, str] | None = None,
        raw_body: str | None = None,
        request_id: str | None = None,
        received_at: str | None = None,
    ) -> None:
        if event_id is not None:
            await handle_webhook_process(event_id=event_id, client_ip=client_ip or "")
            return
        await handle_webhook_ingest(
            source=source or "unknown",
            raw_headers=raw_headers or {},
            raw_body=raw_body or "",
            client_ip=client_ip or "",
            request_id=request_id,
            received_at=received_at,
        )

    monkeypatch.setattr(cast(Any, process_webhook_task), "kiq", run_task_inline)

    payload = {
        "alert_name": "checkout-5xx",
        "event_type": "prometheus_alert",
        "service": "checkout-api",
        "message": "5xx rate > 10%",
    }
    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/webhook/prometheus", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert body["success"] is True
    assert body["event_id"] == 0
    assert body["request_id"]

    async with integration_session_factory() as session:
        event = (await session.execute(select(WebhookEvent))).scalar_one_or_none()
        assert event is not None
        assert event.request_id == body["request_id"]
        assert event.processing_status == "completed"
        assert event.importance == "high"
        assert event.ai_analysis is not None
        assert event.ai_analysis["summary"] == "订单服务错误率升高: checkout-5xx"
        assert event.parsed_data is not None
        assert event.parsed_data["alert_name"] == "checkout-5xx"
        assert event.last_notified_at is not None

        rows = (await session.execute(select(WebhookEvent))).scalars().all()
        assert len(rows) == 1

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        detail = await client.get(f"/api/webhooks/by-request/{body['request_id']}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["success"] is True
    assert detail_body["data"]["request_id"] == body["request_id"]

    assert len(posted) == 1
    outbound = posted[0]
    assert outbound["url"] == Config.ai.FORWARD_URL
    card = outbound["json"]
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "red"
    elements_text = str(card["card"]["elements"])
    assert "**时间**\\n—" not in elements_text
    assert "checkout-5xx" in elements_text
    assert "订单服务错误率升高" in elements_text
    assert "回滚最近发布" in elements_text


async def test_mark_webhook_suppressed_does_not_run_duplicate_query(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import WebhookEvent
    from services.webhooks import command_service
    from services.webhooks.command_service import mark_webhook_suppressed

    async with integration_session_factory.begin() as session:
        event = WebhookEvent(source="prometheus", client_ip="127.0.0.1", processing_status="analyzing")
        session.add(event)
        await session.flush()
        event_id = event.id

    async def fail_check_duplicate(*_: object, **__: object) -> object:
        raise AssertionError("storm suppression must not run duplicate queries")

    monkeypatch.setattr(command_service, "check_duplicate_event", fail_check_duplicate)

    await mark_webhook_suppressed(
        event_id=event_id,
        data={"alert_name": "storm"},
        source="prometheus",
        raw_payload=b'{"alert_name":"storm"}',
        headers={"x-test": "1"},
        client_ip="127.0.0.1",
        ai_analysis={"noise_reduction": {"reason": "alert_processing_lock_timeout"}},
        alert_hash="same-hash",
    )

    async with integration_session_factory() as session:
        updated = await session.get(WebhookEvent, event_id)

    assert updated is not None
    assert updated.processing_status == "completed"
    assert updated.forward_status == "skipped"
    assert updated.alert_hash == "same-hash"
    assert updated.is_duplicate is True


async def test_finalization_persists_event_without_forward_outbox(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import ForwardOutbox, WebhookEvent
    from services.webhooks.forwarding_stage import finalize_analysis_transaction
    from services.webhooks.request_parser import parse_request
    from services.webhooks.types import AnalysisResolution, NoiseReductionContext, WebhookProcessContext

    payload = {
        "alert_name": "checkout-5xx",
        "event_type": "prometheus_alert",
        "service": "checkout-api",
    }
    async with integration_session_factory.begin() as session:
        event = WebhookEvent(source="prometheus", client_ip="127.0.0.1", processing_status="analyzing")
        session.add(event)
        await session.flush()
        event_id = event.id

    req_ctx = parse_request(
        "127.0.0.1",
        {},
        payload,
        b'{"alert_name":"checkout-5xx"}',
        "prometheus",
        None,
    )
    alert_hash = WebhookEvent.generate_hash(req_ctx.parsed_data, req_ctx.source)
    ctx = WebhookProcessContext(
        event_id=event_id,
        request_id="req-finalize-test",
        client_ip="127.0.0.1",
        metric_source="prometheus",
        req_ctx=req_ctx,
        alert_hash=alert_hash,
    )
    analysis_res = AnalysisResolution(
        {"importance": "high", "summary": "should rollback", "event_type": "test"},
        True,
        False,
        None,
        False,
    )
    noise = NoiseReductionContext("standalone", None, 0.0, False, "test", 0, [])

    save_res, fwd_dec = await finalize_analysis_transaction(
        ctx,
        analysis_res,
        {"importance": "high", "summary": "should persist"},
        noise,
    )

    async with integration_session_factory() as session:
        updated_event = await session.get(WebhookEvent, event_id)
        outboxes = (await session.execute(select(ForwardOutbox))).scalars().all()

    assert save_res.webhook_id == event_id
    assert fwd_dec is not None
    assert updated_event is not None
    assert updated_event.processing_status == "completed"
    assert updated_event.ai_analysis is not None
    assert updated_event.ai_analysis["summary"] == "should persist"
    assert outboxes == []


async def test_reused_analysis_still_runs_forwarding_decision(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import Config
    from models import ForwardOutbox, WebhookEvent
    from services.webhooks.forwarding_stage import finalize_analysis_transaction
    from services.webhooks.request_parser import parse_request
    from services.webhooks.types import AnalysisResolution, NoiseReductionContext, WebhookProcessContext

    monkeypatch.setattr(Config, "_overrides", dict(Config._overrides))
    monkeypatch.setattr(Config, "_meta", dict(Config._meta))
    Config.set_override("FORWARD_URL", "https://example.com/hook", source="test")
    Config.set_override("ENABLE_PERIODIC_REMINDER", True, source="test")
    Config.set_override("REMINDER_INTERVAL_HOURS", 1, source="test")
    Config.set_override("NOTIFICATION_COOLDOWN_SECONDS", 1, source="test")
    Config.set_override("FORWARD_DUPLICATE_ALERTS", False, source="test")

    async with integration_session_factory.begin() as session:
        original = WebhookEvent(
            source="prometheus",
            client_ip="127.0.0.1",
            processing_status="completed",
            alert_hash="original-reuse-hash",
            ai_analysis={"importance": "high", "summary": "reused"},
            importance="high",
            is_duplicate=False,
            duplicate_count=1,
            last_notified_at=datetime.now() - timedelta(hours=2),
        )
        event = WebhookEvent(source="prometheus", client_ip="127.0.0.1", processing_status="analyzing")
        session.add_all([original, event])
        await session.flush()
        event_id = event.id

    payload = {"alert_name": "checkout-5xx", "event_type": "prometheus_alert", "service": "checkout-api"}
    req_ctx = parse_request("127.0.0.1", {}, payload, b'{"alert_name":"checkout-5xx"}', "prometheus", None)
    ctx = WebhookProcessContext(
        event_id=event_id,
        request_id="req-reuse-test",
        client_ip="127.0.0.1",
        metric_source="prometheus",
        req_ctx=req_ctx,
        alert_hash="reuse-hash",
    )
    analysis_res = AnalysisResolution(
        {"importance": "high", "summary": "reused", "_route_type": "db_reuse"},
        reanalyzed=False,
        is_duplicate=True,
        original_event=original,
        beyond_window=False,
        is_reused=True,
    )
    noise = NoiseReductionContext("standalone", None, 0.0, False, "reuse", 0, [])

    save_res, fwd_dec = await finalize_analysis_transaction(
        ctx,
        analysis_res,
        {"importance": "high", "summary": "reused", "_route_type": "db_reuse"},
        noise,
    )

    async with integration_session_factory() as session:
        outboxes = (await session.execute(select(ForwardOutbox))).scalars().all()

    assert save_res.is_duplicate is True
    assert fwd_dec is not None
    assert fwd_dec.should_forward is True
    assert fwd_dec.is_periodic_reminder is True
    assert outboxes == []


async def test_recovery_scan_requeues_due_retry_without_incrementing_retry_count(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import WebhookEvent
    from services.operations.recovery_poller import run_recovery_scan
    from services.operations.tasks import process_webhook_task

    async with integration_session_factory.begin() as session:
        event = WebhookEvent(
            source="prometheus",
            client_ip="127.0.0.1",
            processing_status="retry",
            retry_count=2,
            next_retry_at=datetime.now() - timedelta(seconds=5),
        )
        session.add(event)
        await session.flush()
        event_id = event.id

    enqueued: list[dict[str, object]] = []

    async def fake_kiq(event_id: int, client_ip: str | None = None) -> None:
        enqueued.append({"event_id": event_id, "client_ip": client_ip})

    monkeypatch.setattr(cast(Any, process_webhook_task), "kiq", fake_kiq)

    await run_recovery_scan(stuck_threshold_seconds=300)

    async with integration_session_factory() as session:
        updated = await session.get(WebhookEvent, event_id)

    assert enqueued == [{"event_id": event_id, "client_ip": "recovery"}]
    assert updated is not None
    assert updated.processing_status == "retry"
    assert updated.retry_count == 2
    assert updated.next_retry_at is not None
    assert updated.next_retry_at > datetime.now()


async def test_recovery_scan_claims_stale_analyzing_event(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import WebhookEvent
    from services.operations.recovery_poller import run_recovery_scan
    from services.operations.tasks import process_webhook_task

    async with integration_session_factory.begin() as session:
        event = WebhookEvent(
            source="prometheus",
            client_ip="127.0.0.1",
            processing_status="analyzing",
            retry_count=0,
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=2),
        )
        session.add(event)
        await session.flush()
        event_id = event.id

    enqueued: list[dict[str, object]] = []

    async def fake_kiq(event_id: int, client_ip: str | None = None) -> None:
        enqueued.append({"event_id": event_id, "client_ip": client_ip})

    monkeypatch.setattr(cast(Any, process_webhook_task), "kiq", fake_kiq)

    await run_recovery_scan(stuck_threshold_seconds=300)

    async with integration_session_factory() as session:
        updated = await session.get(WebhookEvent, event_id)

    assert enqueued == [{"event_id": event_id, "client_ip": "recovery"}]
    assert updated is not None
    assert updated.processing_status == "retry"
    assert updated.retry_count == 1
    assert updated.failure_reason == "stuck_recovery"
    assert updated.next_retry_at is not None
    assert updated.next_retry_at > datetime.now()


async def test_recovery_scan_does_not_requeue_recently_updated_old_event(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import WebhookEvent
    from services.operations.recovery_poller import run_recovery_scan
    from services.operations.tasks import process_webhook_task

    async with integration_session_factory.begin() as session:
        event = WebhookEvent(
            source="prometheus",
            client_ip="127.0.0.1",
            processing_status="analyzing",
            retry_count=0,
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now(),
        )
        session.add(event)
        await session.flush()
        event_id = event.id

    enqueued: list[dict[str, object]] = []

    async def fake_kiq(event_id: int, client_ip: str | None = None) -> None:
        enqueued.append({"event_id": event_id, "client_ip": client_ip})

    monkeypatch.setattr(cast(Any, process_webhook_task), "kiq", fake_kiq)

    await run_recovery_scan(stuck_threshold_seconds=300)

    async with integration_session_factory() as session:
        updated = await session.get(WebhookEvent, event_id)

    assert enqueued == []
    assert updated is not None
    assert updated.processing_status == "analyzing"
    assert updated.retry_count == 0
