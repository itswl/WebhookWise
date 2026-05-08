from __future__ import annotations

from collections.abc import AsyncIterator
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
    from services.webhooks.pipeline import handle_webhook_process

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

    monkeypatch.setattr("services.webhooks.pipeline.analyze_webhook_with_ai", fake_analyze_webhook_with_ai)

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

    async def run_task_inline(event_id: int, client_ip: str | None = None) -> None:
        await handle_webhook_process(event_id=event_id, client_ip=client_ip or "")

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
    event_id = body["event_id"]

    async with integration_session_factory() as session:
        event = await session.get(WebhookEvent, event_id)
        assert event is not None
        assert event.processing_status == "completed"
        assert event.importance == "high"
        assert event.ai_analysis is not None
        assert event.ai_analysis["summary"] == "订单服务错误率升高: checkout-5xx"
        assert event.parsed_data is not None
        assert event.parsed_data["alert_name"] == "checkout-5xx"

        rows = (await session.execute(select(WebhookEvent))).scalars().all()
        assert len(rows) == 1

    assert len(posted) == 1
    outbound = posted[0]
    assert outbound["url"] == Config.ai.FORWARD_URL
    card = outbound["json"]
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "red"
    elements_text = str(card["card"]["elements"])
    assert "checkout-5xx" in elements_text
    assert "订单服务错误率升高" in elements_text
    assert "回滚最近发布" in elements_text
