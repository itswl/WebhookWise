from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from core.datetime_utils import utcnow
from services.dedup import generate_alert_hash


def _set_config(monkeypatch: pytest.MonkeyPatch, config: Any, key: str, value: object) -> None:
    from core.config.manager import get_config_keys

    config_info = get_config_keys()[key]
    monkeypatch.setattr(getattr(config, config_info["sub"]), key, value)


@pytest.fixture()
async def integration_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import models  # noqa: F401 - register all SQLAlchemy models
    from core.app_context import AppContext, set_default_app_context
    from db.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    context = AppContext()
    context.db_engine = engine
    context.session_factory = session_factory
    set_default_app_context(context)

    yield session_factory

    set_default_app_context(None)
    await engine.dispose()


async def test_webhook_receive_to_feishu_card_flow(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    inline_webhook_task_runner: object,
) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "false")

    from api.app import app
    from core.app_context import get_default_app_context
    from models import WebhookEvent
    from services.forwarding.outbox import process_forward_outbox_by_id
    from services.operations.tasks import process_forward_outbox_task

    context = get_default_app_context()
    assert context is not None
    config = context.config
    app.state.app_context.config = config
    monkeypatch.setattr(config.security, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(config.security, "REQUIRE_WEBHOOK_AUTH", False)
    monkeypatch.setattr(config.security, "API_KEY", "integration-read-token")
    monkeypatch.setattr(config.security, "WEBHOOK_RATE_LIMIT_PER_MINUTE", 0)
    _set_config(monkeypatch, config, "ENABLE_ALERT_NOISE_REDUCTION", False)

    forward_target_url = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
    async with integration_session_factory.begin() as setup_session:
        from models import ForwardRule

        setup_session.add(
            ForwardRule(
                name="integration-default",
                enabled=True,
                priority=0,
                match_event_type="",
                match_importance="high",
                match_source="",
                match_duplicate="all",
                match_payload="",
                target_type="feishu",
                target_url=forward_target_url,
                stop_on_match=False,
            )
        )
        from services.forwarding.rules import invalidate_forward_rules_cache

        invalidate_forward_rules_cache()

    async def fake_analyze_webhook_with_ai(webhook_data: dict[str, Any], **_: object) -> dict[str, Any]:
        parsed = webhook_data["parsed_data"]
        return {
            "importance": "high",
            "summary": f"订单服务错误率升高: {parsed['alert_name']}",
            "impact_scope": "checkout-api",
            "actions": ["检查 5xx 日志", "回滚最近发布"],
            "event_type": "integration_test_alert",
        }

    monkeypatch.setattr("services.analysis.ai_analyzer.analyze_webhook_with_ai", fake_analyze_webhook_with_ai)

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

    monkeypatch.setattr("core.http_client.get_http_client", lambda: FakeHttpClient())

    async def accept_url(url: str) -> str:
        return url

    monkeypatch.setattr("core.url_security.validate_outbound_url", accept_url)

    async def run_outbox_inline(outbox_id: int) -> None:
        await process_forward_outbox_by_id(outbox_id)

    monkeypatch.setattr(cast(Any, process_forward_outbox_task), "kiq", run_outbox_inline)

    payload = {
        "alert_name": "checkout-5xx",
        "event_type": "prometheus_alert",
        "service": "checkout-api",
        "message": "5xx rate > 10%",
    }
    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/v1/webhook/prometheus", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["event_id"] is None
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
        detail = await client.get(
            f"/v1/webhooks/by-request/{body['request_id']}",
            headers={"Authorization": "Bearer integration-read-token"},
        )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["success"] is True
    assert detail_body["data"]["request_id"] == body["request_id"]

    assert len(posted) == 1
    outbound = posted[0]
    assert outbound["url"] == forward_target_url
    card = outbound["json"]
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "red"
    elements_text = str(card["card"]["elements"])
    assert "**时间**\\n—" not in elements_text
    assert "checkout-5xx" in elements_text
    assert "订单服务错误率升高" in elements_text
    assert "建议操作" not in elements_text
    assert "回滚最近发布" not in elements_text


async def test_finalization_skips_outbox_without_target(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import ForwardOutbox, WebhookEvent
    from services.dedup import DedupResult
    from services.webhooks.forwarding_stage import finalize_analysis_transaction
    from services.webhooks.pipeline import parse_request
    from services.webhooks.types import NoiseReductionContext, WebhookProcessContext

    payload = {
        "alert_name": "checkout-5xx",
        "event_type": "prometheus_alert",
        "service": "checkout-api",
    }
    req_ctx = parse_request(
        "127.0.0.1",
        {},
        payload,
        b'{"alert_name":"checkout-5xx"}',
        "prometheus",
        None,
    )
    alert_hash = generate_alert_hash(req_ctx.parsed_data, req_ctx.source)
    ctx = WebhookProcessContext(
        event_id=None,
        request_id="req-finalize-test",
        metric_source="prometheus",
        req_ctx=req_ctx,
        alert_hash=alert_hash,
        dedup_key=alert_hash,
    )
    analysis_res = DedupResult(
        action="new",
        analysis={"importance": "high", "summary": "should rollback", "event_type": "test"},
        original_event_id=None,
    )
    noise = NoiseReductionContext("standalone", None, 0.0, False, "test", 0, ())

    finalize_res = await finalize_analysis_transaction(
        ctx,
        analysis_res,
        {"importance": "high", "summary": "should persist"},
        noise,
    )
    save_res, fwd_dec = finalize_res.save_result, finalize_res.forward_decision

    async with integration_session_factory() as session:
        outboxes = (await session.execute(select(ForwardOutbox))).scalars().all()

    assert fwd_dec is not None
    assert fwd_dec.should_forward is False
    assert finalize_res.outbox_ids == []
    async with integration_session_factory() as session:
        saved_event = await session.get(WebhookEvent, save_res.webhook_id)
    assert saved_event is not None
    assert saved_event.processing_status == "completed"
    assert saved_event.ai_analysis is not None
    assert saved_event.ai_analysis["summary"] == "should persist"
    assert outboxes == []


async def test_save_webhook_is_idempotent_for_existing_request_id(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import WebhookEvent
    from services.webhooks.command_service import SaveWebhookInput, save_webhook_data_in_session

    async with integration_session_factory.begin() as session:
        existing = WebhookEvent(
            source="prometheus",
            request_id="req-idempotent",
            client_ip="127.0.0.1",
            processing_status="completed",
            parsed_data={"alert_name": "checkout-5xx"},
            ai_analysis={"importance": "high", "summary": "already persisted"},
            importance="high",
            is_duplicate=False,
            duplicate_count=1,
        )
        session.add(existing)
        await session.flush()
        existing_id = existing.id

        saved = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(
                data={"alert_name": "checkout-5xx"},
                source="prometheus",
                request_id="req-idempotent",
                ai_analysis={"importance": "low", "summary": "should not overwrite"},
                alert_hash="same-hash",
            ),
        )

    async with integration_session_factory() as session:
        rows = (await session.execute(select(WebhookEvent))).scalars().all()
        persisted = await session.get(WebhookEvent, existing_id)

    assert saved.webhook_id == existing_id
    assert len(rows) == 1
    assert persisted is not None
    assert persisted.ai_analysis is not None
    assert persisted.ai_analysis["summary"] == "already persisted"


async def test_data_maintenance_archives_old_events_before_delete(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import ArchivedWebhookEvent, WebhookEvent
    from services.operations.data_maintenance import cleanup_old_data_by_policy
    from services.operations.policies import DataMaintenancePolicy

    old_timestamp = utcnow() - timedelta(days=40)
    fresh_timestamp = utcnow() - timedelta(days=1)
    async with integration_session_factory.begin() as session:
        old_event = WebhookEvent(
            request_id="req-old-archive",
            source="prometheus",
            client_ip="127.0.0.1",
            timestamp=old_timestamp,
            raw_payload=b'{"alert_name":"old"}',
            headers={"x-source": "test"},
            parsed_data={"alert_name": "old"},
            alert_hash="old-archive-hash",
            ai_analysis={"importance": "low", "summary": "old should archive"},
            importance="low",
            processing_status="completed",
            forward_status="sent",
            is_duplicate=False,
            duplicate_count=1,
        )
        fresh_event = WebhookEvent(
            request_id="req-fresh-archive",
            source="prometheus",
            client_ip="127.0.0.1",
            timestamp=fresh_timestamp,
            parsed_data={"alert_name": "fresh"},
            ai_analysis={"importance": "high", "summary": "fresh should remain"},
            importance="high",
            processing_status="completed",
        )
        session.add_all([old_event, fresh_event])
        await session.flush()
        old_id = old_event.id
        fresh_id = fresh_event.id

    archived_count = await cleanup_old_data_by_policy(
        policy=DataMaintenancePolicy(
            enabled=True,
            retention_days_default=30,
            retention_policies={},
            source_retention_policies={},
            cleanup_keywords={},
        )
    )

    async with integration_session_factory() as session:
        remaining = (await session.execute(select(WebhookEvent).order_by(WebhookEvent.id.asc()))).scalars().all()
        archived = (
            await session.execute(select(ArchivedWebhookEvent).order_by(ArchivedWebhookEvent.id.asc()))
        ).scalar_one()

    assert archived_count == 1
    assert [event.id for event in remaining] == [fresh_id]
    assert archived.id == old_id
    assert archived.request_id == "req-old-archive"
    assert archived.raw_payload == b'{"alert_name":"old"}'
    assert archived.headers == {"x-source": "test"}
    assert archived.parsed_data == {"alert_name": "old"}
    assert archived.ai_analysis == {"importance": "low", "summary": "old should archive"}
    assert archived.archived_at is not None


async def test_data_maintenance_uses_importance_before_source_and_default_retention(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import ArchivedWebhookEvent, WebhookEvent
    from services.operations.data_maintenance import cleanup_old_data_by_policy
    from services.operations.policies import DataMaintenancePolicy

    old_timestamp = utcnow() - timedelta(days=40)
    async with integration_session_factory.begin() as session:
        high_prometheus = WebhookEvent(
            request_id="req-high-prometheus-retained",
            source="prometheus",
            timestamp=old_timestamp,
            parsed_data={"alert_name": "important"},
            ai_analysis={"importance": "high", "summary": "important production alert"},
            importance="high",
            processing_status="completed",
        )
        source_only = WebhookEvent(
            request_id="req-source-policy-archived",
            source="prometheus",
            timestamp=old_timestamp,
            parsed_data={"alert_name": "source-only"},
            ai_analysis={"importance": "custom", "summary": "source policy applies"},
            importance="custom",
            processing_status="completed",
        )
        source_without_importance = WebhookEvent(
            request_id="req-source-null-importance-archived",
            source="prometheus",
            timestamp=old_timestamp,
            parsed_data={"alert_name": "source-null-importance"},
            ai_analysis={"summary": "source policy applies without importance"},
            importance=None,
            processing_status="completed",
        )
        default_only = WebhookEvent(
            request_id="req-default-policy-archived",
            source="custom",
            timestamp=old_timestamp,
            parsed_data={"alert_name": "default-only"},
            ai_analysis={"importance": "custom", "summary": "default policy applies"},
            importance="custom",
            processing_status="completed",
        )
        session.add_all([high_prometheus, source_only, source_without_importance, default_only])

    archived_count = await cleanup_old_data_by_policy(
        policy=DataMaintenancePolicy(
            enabled=True,
            retention_days_default=30,
            retention_policies={"high": 90, "medium": 30, "low": 7, "unknown": 3},
            source_retention_policies={"prometheus": 30},
            cleanup_keywords={},
        )
    )

    async with integration_session_factory() as session:
        remaining_request_ids = (
            await session.scalars(select(WebhookEvent.request_id).order_by(WebhookEvent.request_id.asc()))
        ).all()
        archived_request_ids = (
            await session.scalars(
                select(ArchivedWebhookEvent.request_id).order_by(ArchivedWebhookEvent.request_id.asc())
            )
        ).all()

    assert archived_count == 3
    assert remaining_request_ids == ["req-high-prometheus-retained"]
    assert archived_request_ids == [
        "req-default-policy-archived",
        "req-source-null-importance-archived",
        "req-source-policy-archived",
    ]


async def test_original_id_only_duplicate_save_uses_incremented_duplicate_count(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import WebhookEvent
    from services.webhooks.command_service import SaveWebhookInput, save_webhook_data_in_session

    async with integration_session_factory.begin() as session:
        original = WebhookEvent(
            source="volcengine",
            client_ip="127.0.0.1",
            processing_status="completed",
            alert_hash="redis-reuse-count-hash",
            parsed_data={"RuleId": "disk"},
            ai_analysis={"importance": "high", "summary": "disk high"},
            importance="high",
            is_duplicate=False,
            duplicate_count=41,
        )
        session.add(original)
        await session.flush()
        original_id = original.id

        saved = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(
                data={"RuleId": "disk"},
                source="volcengine",
                request_id="redis-reuse-count-request",
                ai_analysis={"importance": "high", "summary": "disk still high"},
                alert_hash="redis-reuse-count-hash",
                is_duplicate=True,
                original_event_id=original_id,
                skip_duplicate_lookup=True,
            ),
        )

    async with integration_session_factory() as session:
        persisted_original = await session.get(WebhookEvent, original_id)
        duplicate = await session.get(WebhookEvent, saved.webhook_id)

    assert persisted_original is not None
    assert duplicate is not None
    assert persisted_original.duplicate_count == 42
    assert duplicate.duplicate_count == 42
    assert duplicate.duplicate_of == original_id


async def test_reused_analysis_queues_periodic_forward_outbox(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.app_context import get_default_app_context
    from models import ForwardOutbox, WebhookEvent
    from services.dedup import DedupResult
    from services.webhooks.forwarding_stage import finalize_analysis_transaction
    from services.webhooks.pipeline import parse_request
    from services.webhooks.types import NoiseReductionContext, WebhookProcessContext

    context = get_default_app_context()
    assert context is not None
    config = context.config
    _set_config(monkeypatch, config, "ENABLE_PERIODIC_REMINDER", True)
    _set_config(monkeypatch, config, "REMINDER_INTERVAL_HOURS", 1)
    _set_config(monkeypatch, config, "NOTIFICATION_COOLDOWN_SECONDS", 1)
    _set_config(monkeypatch, config, "FORWARD_DUPLICATE_ALERTS", False)

    async with integration_session_factory.begin() as setup_session:
        from models import ForwardRule

        setup_session.add(
            ForwardRule(
                name="reuse-default",
                enabled=True,
                priority=0,
                match_event_type="",
                match_importance="high",
                match_source="",
                match_duplicate="all",
                match_payload="",
                target_type="webhook",
                target_url="https://example.com/hook",
                stop_on_match=False,
            )
        )
        from services.forwarding.rules import invalidate_forward_rules_cache

        invalidate_forward_rules_cache()

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
            last_notified_at=utcnow() - timedelta(hours=2),
        )
        session.add(original)
        await session.flush()
        original_id = original.id

    payload = {"alert_name": "checkout-5xx", "event_type": "prometheus_alert", "service": "checkout-api"}
    req_ctx = parse_request("127.0.0.1", {}, payload, b'{"alert_name":"checkout-5xx"}', "prometheus", None)
    ctx = WebhookProcessContext(
        event_id=None,
        request_id="req-reuse-test",
        metric_source="prometheus",
        req_ctx=req_ctx,
        alert_hash="reuse-hash",
        dedup_key="reuse-hash",
    )
    analysis_res = DedupResult(
        action="reuse",
        analysis={"importance": "high", "summary": "reused", "_route_type": "db_reuse"},
        original_event_id=original_id,
        route_type="db_reuse",
    )
    noise = NoiseReductionContext("standalone", None, 0.0, False, "reuse", 0, ())

    finalize_res = await finalize_analysis_transaction(
        ctx,
        analysis_res,
        {"importance": "high", "summary": "reused", "_route_type": "db_reuse"},
        noise,
    )
    save_res, fwd_dec = finalize_res.save_result, finalize_res.forward_decision

    async with integration_session_factory() as session:
        outboxes = (await session.execute(select(ForwardOutbox))).scalars().all()

    assert save_res.is_duplicate is True
    assert fwd_dec is not None
    assert fwd_dec.should_forward is True
    assert fwd_dec.is_periodic_reminder is True
    assert len(outboxes) == 1
    assert finalize_res.outbox_ids == [outboxes[0].id]
    assert outboxes[0].webhook_event_id == save_res.webhook_id
    assert outboxes[0].target_url == "https://example.com/hook"
