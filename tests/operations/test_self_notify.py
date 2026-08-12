"""Out-of-band self-notification: gating, payload shapes, fail-open, wiring."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.operations import self_notify


@pytest.fixture(autouse=True)
def _reset_gate():
    self_notify._reset_gate_for_tests()
    yield
    self_notify._reset_gate_for_tests()


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


class _FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.response = _FakeResponse()

    async def post(self, url: str, json: dict[str, Any], timeout: float) -> _FakeResponse:
        self.posts.append((url, json))
        return self.response


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> _FakeHttpClient:
    client = _FakeHttpClient()
    monkeypatch.setattr("core.http_client.get_http_client", lambda: client)
    return client


@pytest.fixture
def fake_redis_gate(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """In-memory stand-ins for the redis gate/counter helpers."""
    state: dict[str, Any] = {"gate_held": False, "suppressed": 0}

    async def set_nx_ex(key: str, value: str, ttl: int) -> bool:
        if state["gate_held"]:
            return False
        state["gate_held"] = True
        return True

    async def incr(key: str, ttl: int) -> int:
        state["suppressed"] += 1
        return state["suppressed"]

    async def get_str(key: str) -> str | None:
        return str(state["suppressed"]) if state["suppressed"] else None

    async def delete(key: str) -> int:
        state["suppressed"] = 0
        return 1

    monkeypatch.setattr("core.redis_client.redis_set_nx_ex", set_nx_ex)
    monkeypatch.setattr("core.redis_client.redis_incr_with_expire", incr)
    monkeypatch.setattr("core.redis_client.redis_get_str", get_str)
    monkeypatch.setattr("core.redis_client.redis_delete", delete)
    return state


@pytest.mark.asyncio
async def test_disabled_without_url(fake_http: _FakeHttpClient) -> None:
    assert await self_notify.notify_delivery_exhausted(target_type="feishu", error="boom", outbox_id=1) is False
    assert fake_http.posts == []


@pytest.mark.asyncio
async def test_sends_feishu_payload_then_gates_then_folds_suppressed(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any, fake_http: _FakeHttpClient, fake_redis_gate: dict[str, Any]
) -> None:
    monkeypatch.setattr(temp_config.notifications, "SELF_NOTIFY_WEBHOOK_URL", "https://fallback.example/hook")

    assert await self_notify.notify_delivery_exhausted(target_type="feishu", error="err-1", outbox_id=7, event_id=42)
    url, payload = fake_http.posts[0]
    assert url == "https://fallback.example/hook"
    assert payload["msg_type"] == "text"
    text = payload["content"]["text"]
    assert "outbox_id=7" in text and "event_id=42" in text and "err-1" in text

    # Gate held: second failure is suppressed and counted, not posted.
    assert await self_notify.notify_delivery_exhausted(target_type="feishu", error="err-2", outbox_id=8) is False
    assert len(fake_http.posts) == 1
    assert fake_redis_gate["suppressed"] == 1

    # Gate reopens: the next notice folds in the suppressed count.
    fake_redis_gate["gate_held"] = False
    assert await self_notify.notify_delivery_exhausted(target_type="feishu", error="err-3", outbox_id=9)
    assert "另有 1 次" in fake_http.posts[1][1]["content"]["text"]
    assert fake_redis_gate["suppressed"] == 0


@pytest.mark.asyncio
async def test_generic_kind_posts_structured_json(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any, fake_http: _FakeHttpClient, fake_redis_gate: dict[str, Any]
) -> None:
    monkeypatch.setattr(temp_config.notifications, "SELF_NOTIFY_WEBHOOK_URL", "https://fallback.example/hook")
    monkeypatch.setattr(temp_config.notifications, "SELF_NOTIFY_KIND", "generic")

    assert await self_notify.notify_delivery_exhausted(target_type="deep_analysis", error="x", outbox_id=3)
    _, payload = fake_http.posts[0]
    assert payload["source"] == "webhookwise"
    assert payload["type"] == "delivery_exhausted"
    assert payload["target_type"] == "deep_analysis"


@pytest.mark.asyncio
async def test_redis_down_falls_back_to_in_process_gate(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any, fake_http: _FakeHttpClient
) -> None:
    monkeypatch.setattr(temp_config.notifications, "SELF_NOTIFY_WEBHOOK_URL", "https://fallback.example/hook")

    async def boom(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("redis down")

    monkeypatch.setattr("core.redis_client.redis_set_nx_ex", boom)

    # Redis down must not silence the last-resort channel...
    assert await self_notify.notify_delivery_exhausted(target_type="feishu", error="x", outbox_id=1) is True
    # ...but the per-process gate still prevents a same-window spam loop.
    assert await self_notify.notify_delivery_exhausted(target_type="feishu", error="x", outbox_id=2) is False
    assert len(fake_http.posts) == 1


@pytest.mark.asyncio
async def test_http_failure_never_raises(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any, fake_redis_gate: dict[str, Any]
) -> None:
    monkeypatch.setattr(temp_config.notifications, "SELF_NOTIFY_WEBHOOK_URL", "https://fallback.example/hook")

    class _ExplodingClient:
        async def post(self, url: str, json: dict[str, Any], timeout: float) -> _FakeResponse:
            raise OSError("network unreachable")

    monkeypatch.setattr("core.http_client.get_http_client", lambda: _ExplodingClient())
    assert await self_notify.notify_delivery_exhausted(target_type="feishu", error="x", outbox_id=1) is False


@pytest.mark.asyncio
async def test_outbox_exhaustion_triggers_self_notify(
    monkeypatch: pytest.MonkeyPatch,
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exhausted branch calls the fallback even for outbox_exhausted meta-cards."""
    from models import ForwardOutbox
    from services.forwarding import outbox as outbox_mod

    calls: list[dict[str, Any]] = []

    async def record_call(**kwargs: Any) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr("services.operations.self_notify.notify_delivery_exhausted", record_call)

    async with db_app_context_session_factory.begin() as session:
        record = ForwardOutbox(
            idempotency_key="self-notify-wire-1",
            target_type="feishu",
            event_type="outbox_exhausted",  # the meta-card itself dying
            status="processing",
            attempts=3,
            max_attempts=3,
        )
        session.add(record)
        await session.flush()
        outbox_id = int(record.id)

    await outbox_mod._finalize_outbox_failure(outbox_id, "still down", permanent=True)

    assert len(calls) == 1
    assert calls[0]["target_type"] == "feishu"
    assert calls[0]["outbox_id"] == outbox_id
