"""Read-only MCP server: tool wiring over a real sqlite session + auth guard."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture()
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import models  # noqa: F401
    from db.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def patch_session_scope(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Point the MCP tools' session_scope at the test engine."""
    from api.mcp import server

    @contextlib.asynccontextmanager
    async def _scope(existing: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
        async with session_factory() as sess:
            yield sess

    monkeypatch.setattr(server, "session_scope", _scope)


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    from models import DecisionTrace, ForwardRule, WebhookEvent

    async with factory() as s:
        s.add_all(
            [
                WebhookEvent(id=1, source="grafana", importance="high", processing_status="completed"),
                WebhookEvent(id=2, source="prometheus", importance="low", processing_status="dead_letter",
                             failure_reason="boom", error_message="connect timeout"),
                ForwardRule(name="busy", target_type="feishu", target_url="https://x/hook/a", enabled=True),
                DecisionTrace(webhook_event_id=1, outcome="forwarded", skip_code="none", matched_rules=["busy"]),
                DecisionTrace(webhook_event_id=2, outcome="skipped", skip_code="silenced", matched_rules=None),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_tools_are_registered() -> None:
    from api.mcp.server import mcp_server

    names = {t.name for t in await mcp_server.list_tools()}
    assert names == {
        "get_alert_decision_trace",
        "list_alert_decision_traces",
        "list_recent_alerts",
        "get_alert_overview_stats",
        "get_forward_rule_roi",
        "list_dead_letter_alerts",
        "get_dead_letter_alert",
    }


@pytest.mark.asyncio
async def test_get_alert_decision_trace(
    patch_session_scope: None, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from api.mcp import server

    await _seed(session_factory)
    trace = await server.get_alert_decision_trace(webhook_event_id=1)
    assert trace is not None
    assert trace["outcome"] == "forwarded"
    # Unknown event → None (not an error).
    assert await server.get_alert_decision_trace(webhook_event_id=999) is None


@pytest.mark.asyncio
async def test_list_decision_traces_filters_and_clamps(
    patch_session_scope: None, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from api.mcp import server

    await _seed(session_factory)
    res = await server.list_alert_decision_traces(outcome="skipped", page_size=9999)
    assert [i["outcome"] for i in res["items"]] == ["skipped"]
    assert "has_more" in res and "next_cursor" in res
    # An out-of-enum outcome is coerced to "no filter" rather than passed through.
    res_all = await server.list_alert_decision_traces(outcome="bogus")
    assert len(res_all["items"]) == 2


@pytest.mark.asyncio
async def test_recent_alerts_and_overview(
    patch_session_scope: None, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from api.mcp import server

    await _seed(session_factory)
    alerts = await server.list_recent_alerts(importance="high")
    assert [a["source"] for a in alerts["items"]] == ["grafana"]

    overview = await server.get_alert_overview_stats(period="bogus")  # coerced to "day"
    assert overview["period"] == "day"
    assert overview["forwarded"] == 1
    assert overview["skipped"] == 1


@pytest.mark.asyncio
async def test_forward_rule_roi_and_dead_letters(
    patch_session_scope: None, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from api.mcp import server

    await _seed(session_factory)
    roi = await server.get_forward_rule_roi()
    assert roi["busy"]["count"] == 1

    dead = await server.list_dead_letter_alerts()
    assert [d["id"] for d in dead["items"]] == [2]
    detail = await server.get_dead_letter_alert(event_id=2)
    assert detail is not None and detail["failure_reason"] == "boom"
    # A non-dead-letter event returns None.
    assert await server.get_dead_letter_alert(event_id=1) is None


# ── Auth middleware ──────────────────────────────────────────────────────────


def _config_with_key(key: str | None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(security=SimpleNamespace(API_KEY=key))


async def _run_mw(monkeypatch: pytest.MonkeyPatch, *, api_key: str | None, headers: list[tuple[bytes, bytes]]) -> int:
    from api.mcp import auth

    monkeypatch.setattr(auth, "get_config_manager", lambda: _config_with_key(api_key))

    called = {"downstream": False}

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        called["downstream"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = auth.MCPAuthMiddleware(_app)
    status_holder = {"status": 0}

    async def _send(msg: dict[str, Any]) -> None:
        if msg["type"] == "http.response.start":
            status_holder["status"] = msg["status"]

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "headers": headers, "client": ("1.2.3.4", 5)}
    await mw(scope, _receive, _send)
    return status_holder["status"]


@pytest.mark.asyncio
async def test_auth_allows_valid_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    status = await _run_mw(monkeypatch, api_key="secret", headers=[(b"authorization", b"Bearer secret")])
    assert status == 200


@pytest.mark.asyncio
async def test_auth_allows_x_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    status = await _run_mw(monkeypatch, api_key="secret", headers=[(b"x-api-key", b"secret")])
    assert status == 200


@pytest.mark.asyncio
async def test_auth_rejects_bad_and_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _run_mw(monkeypatch, api_key="secret", headers=[(b"authorization", b"Bearer wrong")]) == 401
    assert await _run_mw(monkeypatch, api_key="secret", headers=[]) == 401


@pytest.mark.asyncio
async def test_auth_rejects_when_no_api_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # No API_KEY configured → deny even a matching-looking token (fail closed).
    assert await _run_mw(monkeypatch, api_key=None, headers=[(b"authorization", b"Bearer anything")]) == 401


# ── Mount path (regression: mounting at /mcp must resolve, not /mcp/mcp) ──────


@pytest.mark.asyncio
async def test_mounted_at_mcp_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app is mounted at /mcp; the transport route must be the mount root.

    If streamable_http_path is left at its "/mcp" default, the effective path
    becomes /mcp/mcp and clients hitting /mcp get a 404. This guards that.
    """
    import httpx
    from fastapi import FastAPI
    from mcp.server.transport_security import TransportSecuritySettings

    from api.mcp import auth, build_mcp_app, mcp_server

    monkeypatch.setattr(auth, "get_config_manager", lambda: _config_with_key("secret"))
    # Allow the test client's Host so DNS-rebinding protection doesn't 421 first.
    # Must be set before build_mcp_app() creates the session manager, which
    # captures transport_security at construction time.
    monkeypatch.setattr(
        "api.mcp.server._configure_transport_security",
        lambda: setattr(
            mcp_server.settings,
            "transport_security",
            TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["testserver"],
                allowed_origins=["http://testserver"],
            ),
        ),
    )

    app = FastAPI()
    app.mount("/mcp", build_mcp_app())

    async with mcp_server.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            init_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
            }
            accept = "application/json, text/event-stream"
            authed = await client.post(
                "/mcp/",
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json", "Accept": accept},
                json=init_body,
            )
            # The key assertion: not a 404 (route resolves under the mount).
            assert authed.status_code == 200, authed.status_code
            unauth = await client.post(
                "/mcp/",
                headers={"Content-Type": "application/json", "Accept": accept},
                json=init_body,
            )
            assert unauth.status_code == 401
