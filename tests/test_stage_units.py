from typing import Any, cast

import pytest


def test_parse_request_decodes_raw_json_without_database() -> None:
    from services.webhooks.request_parser import parse_request

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
    from services.notifications.feishu import FeishuNotificationChannel
    from services.operations.policies import FeishuNotificationPolicy

    async def fake_validate(url: str) -> str:
        return url

    class Response:
        status_code = 200

    class Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def post(self, url: str, *, json: object, timeout: float | int | None = None) -> Response:
            self.calls.append({"url": url, "json": json, "timeout": timeout})
            return Response()

    class Breaker:
        async def call_async(self, func: Any, *args: Any, **kwargs: Any) -> object:
            return await func(*args, **kwargs)

    client = Client()
    channel = FeishuNotificationChannel(
        http_client=client,
        circuit_breaker=Breaker(),  # type: ignore[arg-type]
        policy=FeishuNotificationPolicy(timeout_seconds=3),
        validate_url=fake_validate,
    )

    ok = await channel.send_deep_analysis(
        "https://open.feishu.cn/open-apis/bot/v2/hook/token",
        {"analysis_result": {"root_cause": "x", "impact": "y", "confidence": 0.8}, "engine": "openclaw"},
        source="prometheus",
        webhook_event_id=42,
    )

    assert ok is True
    assert client.calls[0]["timeout"] == 3
    assert client.calls[0]["json"]["msg_type"] == "interactive"
    assert "ID: 42" in client.calls[0]["json"]["card"]["elements"][-1]["elements"][0]["content"]


@pytest.mark.asyncio
async def test_feishu_facade_uses_supplied_notification_channel() -> None:
    from services.operations.feishu_notifications import send_feishu_deep_analysis

    class Channel:
        name = "test"

        def __init__(self) -> None:
            self.called = False

        def supports(self, target_url: str) -> bool:
            return target_url == "https://open.feishu.cn/open-apis/bot/v2/hook/token"

        async def send_card(self, target_url: str, card_payload: object) -> bool:
            raise AssertionError("facade should call send_deep_analysis for this path")

        async def send_deep_analysis(
            self,
            target_url: str,
            analysis_record: dict[str, Any],
            *,
            source: str = "",
            webhook_event_id: int = 0,
        ) -> bool:
            self.called = True
            assert source == "grafana"
            assert webhook_event_id == 7
            assert analysis_record["engine"] == "openclaw"
            return True

    channel = Channel()

    ok = await send_feishu_deep_analysis(
        "https://open.feishu.cn/open-apis/bot/v2/hook/token",
        {"analysis_result": {}, "engine": "openclaw"},
        source="grafana",
        webhook_event_id=7,
        channels=[channel],
    )

    assert ok is True
    assert channel.called is True


@pytest.mark.asyncio
async def test_forward_to_remote_uses_injected_dependencies_only() -> None:
    from services.forwarding.dependencies import RemoteForwardDependencies
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


@pytest.mark.asyncio
async def test_resolve_analysis_reuses_redis_dedup_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.analysis_resolution as analysis_resolution
    from services.webhooks.deduplication import CachedDuplicate
    from services.webhooks.repository import DuplicateCheckResult

    async def cached_duplicate(_: str) -> CachedDuplicate:
        return CachedDuplicate(123, {"importance": "high", "summary": "cached"})

    class Original:
        id = 123

    original = Original()

    async def window_lookup(*_: object, **__: object) -> DuplicateCheckResult:
        return DuplicateCheckResult(True, original, False, None)  # type: ignore[arg-type]

    async def fail_ai(*_: object, **__: object) -> object:
        raise AssertionError("cached duplicate should not invoke AI")

    async def noop_usage(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(analysis_resolution, "get_cached_duplicate", cached_duplicate)
    monkeypatch.setattr(analysis_resolution, "check_duplicate_event", window_lookup)
    monkeypatch.setattr(analysis_resolution, "analyze_webhook_with_ai", fail_ai)
    monkeypatch.setattr(analysis_resolution, "log_ai_usage", noop_usage)

    result = await analysis_resolution.resolve_analysis("same-hash", {"source": "prometheus"})

    assert result.is_duplicate is True
    assert result.is_reused is True
    assert result.original_event is original
    assert result.original_event_id == 123
    assert result.analysis_result["_route_type"] == "redis_reuse"
