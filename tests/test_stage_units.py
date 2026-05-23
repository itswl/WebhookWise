from typing import Any, cast

import pytest


def test_parse_request_decodes_raw_json_without_database() -> None:
    from services.webhooks.pipeline import parse_request

    ctx = parse_request(
        client_ip="203.0.113.10",
        headers={"x-webhook-source": "custom"},
        payload={},
        raw_body=b'{"message":"hello"}',
        source=None,
        ts="2026-05-12T00:00:00Z",
    )

    assert ctx.client_ip == "203.0.113.10"
    assert ctx.source == "custom"
    assert ctx.parsed_data == {"message": "hello"}
    assert ctx.webhook_full_data["body"] == {"message": "hello"}


@pytest.mark.asyncio
async def test_feishu_channel_sends_card_through_injected_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.channels.feishu import build_deep_analysis_card

    card = build_deep_analysis_card(
        {"analysis_result": {"root_cause": "x", "impact": "y", "confidence": 0.8}, "engine": "openclaw"},
        source="prometheus",
        webhook_event_id=42,
    )

    assert card["msg_type"] == "interactive"
    assert "ID: 42" in card["card"]["elements"][-1]["elements"][0]["content"]


@pytest.mark.asyncio
async def test_feishu_facade_uses_supplied_notification_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.operations.deep_analysis_notifications import send_feishu_deep_analysis

    enqueued: list[dict[str, object]] = []

    async def fake_enqueue_external_message(**kwargs: object) -> int:
        enqueued.append(dict(kwargs))
        return len(enqueued)

    import services.operations.deep_analysis_notifications as module
    monkeypatch.setattr(module, "enqueue_external_message", fake_enqueue_external_message)

    ok = await send_feishu_deep_analysis(
        "https://open.feishu.cn/open-apis/bot/v2/hook/token",
        {"analysis_result": {}, "engine": "openclaw"},
        source="grafana",
        webhook_event_id=7,
        channels=None,
    )

    assert ok is True
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_forward_to_remote_uses_injected_dependencies_only() -> None:
    from services.forwarding.circuit_breakers import RemoteForwardDependencies
    from services.forwarding.policies import RemoteForwardPolicy
    from services.forwarding.remote import forward_to_remote

    async def accept_url(url: str) -> str:
        return url

    class Response:
        status_code = 200
        content = b'{"ok":true}'
        text = '{"ok":true}'

        def json(self) -> dict[str, bool]:
            return {"ok": True}

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def post(self, url: str, **_: Any) -> Response:
            self.urls.append(url)
            return Response()

    class Breaker:
        def __init__(self) -> None:
            self.called = False

        async def call_async(self, func: Any, *args: Any, **kwargs: Any) -> object:
            self.called = True
            return await func(*args, **kwargs)

    client = Client()
    breaker = Breaker()

    result = await forward_to_remote(
        {"source": "unit", "parsed_data": {}},
        {"summary": "ok"},
        target_url="https://example.test/hook",
        policy=RemoteForwardPolicy(default_target_url="", timeout_seconds=2),
        dependencies=RemoteForwardDependencies(
            http_client=client,
            circuit_breaker=cast(Any, breaker),
            validate_url=accept_url,
        ),
    )

    assert result["status"] == "success"
    assert breaker.called is True
    assert client.urls == ["https://example.test/hook"]


@pytest.mark.asyncio
async def test_ingress_backpressure_suppresses_after_threshold() -> None:
    from services.webhooks.ingress_backpressure import check_ingress_backpressure
    from services.webhooks.policies import WebhookReceivePolicy

    calls = 0

    async def fake_eval(*_: object) -> int:
        nonlocal calls
        calls += 1
        return calls

    policy = WebhookReceivePolicy(
        max_body_bytes=1024,
        ingress_backpressure_threshold=1,
        ingress_backpressure_window_seconds=60,
    )

    first = await check_ingress_backpressure(
        source_hint="prometheus",
        raw_body=b'{"alertname":"HighCPU","instance":"pod-a"}',
        policy=policy,
        redis_eval_int_func=fake_eval,
    )
    second = await check_ingress_backpressure(
        source_hint="prometheus",
        raw_body=b'{"alertname":"HighCPU","instance":"pod-a"}',
        policy=policy,
        redis_eval_int_func=fake_eval,
    )

    assert first.suppressed is False
    assert second.suppressed is True
    assert second.reason == "ingress_storm_backpressure"


@pytest.mark.asyncio
async def test_ingress_backpressure_suppresses_on_redis_error() -> None:
    from services.webhooks.ingress_backpressure import check_ingress_backpressure
    from services.webhooks.policies import WebhookReceivePolicy

    async def failing_eval(*_: object) -> int:
        raise RuntimeError("redis unavailable")

    result = await check_ingress_backpressure(
        source_hint="prometheus",
        raw_body=b'{"alertname":"HighCPU","instance":"a"}',
        policy=WebhookReceivePolicy(
            max_body_bytes=1024,
            ingress_backpressure_threshold=1,
            ingress_backpressure_window_seconds=60,
        ),
        redis_eval_int_func=failing_eval,
    )

    assert result.suppressed is True
    assert result.reason == "redis_unavailable"


@pytest.mark.asyncio
async def test_receive_webhook_suppression_does_not_write_db(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import webhook
    from services.operations.tasks import process_webhook_task
    from services.webhooks.ingress_backpressure import IngressBackpressureResult

    class Request:
        headers: dict[str, str] = {}

        async def body(self) -> bytes:
            return b'{"alertname":"HighCPU"}'

    async def suppressed(*_: object, **__: object) -> IngressBackpressureResult:
        return IngressBackpressureResult(
            suppressed=True,
            key="ingress:webhook:test",
            count=2,
            threshold=1,
            reason="ingress_storm_backpressure",
        )

    async def fail_enqueue(*_: object, **__: object) -> None:
        raise AssertionError("suppressed ingress request must not enqueue work")

    monkeypatch.setattr(webhook, "check_ingress_backpressure", suppressed)
    monkeypatch.setattr(cast(Any, process_webhook_task), "kiq", fail_enqueue)

    result = await webhook._receive_and_enqueue_webhook(
        request=Request(),  # type: ignore[arg-type]
        source_hint="prometheus",
        request_id="req-suppressed",
    )

    assert isinstance(result, dict)
    assert result["event_id"] is None
    assert result["request_id"] == "req-suppressed"
    assert "suppressed" in result["message"]
