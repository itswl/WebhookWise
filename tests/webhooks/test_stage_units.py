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
    from services.notifications.feishu import build_deep_analysis_card

    card = build_deep_analysis_card(
        {"analysis_result": {"root_cause": "x", "impact": "y", "confidence": 0.8}, "engine": "openclaw"},
        source="prometheus",
        webhook_event_id=42,
    )

    assert card["msg_type"] == "interactive"
    assert "ID：42" in card["card"]["elements"][-1]["elements"][0]["content"]


def test_deep_analysis_card_formats_openclaw_json_fence() -> None:
    from services.notifications.feishu import build_deep_analysis_card

    openclaw_text = """```json
{
  "alert_identity": {
    "source": "prometheus",
    "project": "eve",
    "region": "cn-shanghai",
    "service": "openim-msggateway-server",
    "rule_name": "online_user_num 波动率"
  },
  "summary": "在线用户数属于正常早晨流量爬坡，无实际风险。",
  "root_cause": {
    "status": "confirmed",
    "description": "1h 基线窗口在爬坡期偏低，导致波动率告警误报。"
  },
  "impact": {
    "description": "服务正常，无业务影响。"
  },
  "recommendations": [
    {
      "action": "将 avg_over_time[1h] 调整为日周期基线。",
      "reason": "降低早晨爬坡误报"
    }
  ],
  "next_checks": [
    "kubectl logs statefulset/openim-msggateway-server | grep -i 'login\\|connect\\|auth'"
  ],
  "confidence": 0.85
}
```"""
    card = build_deep_analysis_card(
        {"analysis_result": {"root_cause": openclaw_text, "_openclaw_text": openclaw_text}, "engine": "openclaw"},
        source="prometheus",
        webhook_event_id=46708,
    )

    rendered = str(card)
    assert "```json" not in rendered
    assert "在线用户数属于正常早晨流量爬坡" in rendered
    assert "1h 基线窗口" in rendered
    assert "告警标识" in rendered
    assert "ID：46708" in rendered


@pytest.mark.asyncio
async def test_feishu_facade_uses_supplied_notification_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.operations.deep_analysis_notifications import send_feishu_deep_analysis

    enqueued: list[dict[str, object]] = []

    async def fake_forward_notification(**kwargs: object) -> dict[str, object]:
        enqueued.append(dict(kwargs))
        return {"status": "queued", "outbox_id": len(enqueued)}

    import services.operations.deep_analysis_notifications as module

    monkeypatch.setattr(module, "forward_notification", fake_forward_notification)

    ok = await send_feishu_deep_analysis(
        "https://open.feishu.cn/open-apis/bot/v2/hook/token",
        {"analysis_result": {}, "engine": "openclaw"},
        source="grafana",
        webhook_event_id=7,
        importance="high",
        is_duplicate=False,
        parsed_data={"project": "eve-cn", "env": "prod"},
    )

    assert ok is True
    assert len(enqueued) == 1
    assert enqueued[0]["event_type"] == "deep_analysis"
    assert enqueued[0]["importance"] == "high"
    assert enqueued[0]["parsed_data"] == {"project": "eve-cn", "env": "prod"}


@pytest.mark.asyncio
async def test_post_json_to_remote_uses_injected_dependencies_only() -> None:
    from services.forwarding.circuit_breakers import RemoteForwardDependencies
    from services.forwarding.policies import ForwardDeliveryPolicy
    from services.forwarding.remote import post_json_to_remote

    async def accept_url(url: str, **kwargs: Any) -> str:
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

    result = await post_json_to_remote(
        "https://example.test/hook",
        {"webhook": {"source": "unit", "parsed_data": {}}, "analysis": {"summary": "ok"}},
        policy=ForwardDeliveryPolicy(
            timeout_seconds=2,
            max_attempts=3,
            retry_initial_delay=1,
            retry_max_delay=10,
            retry_backoff_multiplier=2.0,
            stale_processing_threshold_seconds=60,
            max_delivery_age_seconds=1800,
        ),
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
async def test_feishu_forward_checks_business_status_code() -> None:
    from services.forwarding.circuit_breakers import RemoteForwardDependencies
    from services.forwarding.policies import ForwardDeliveryPolicy
    from services.forwarding.remote import post_json_to_remote

    async def accept_url(url: str, **kwargs: Any) -> str:
        return url

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"StatusCode": 19001, "StatusMessage": "incoming webhook access token invalid"}

        def raise_for_status(self) -> None:
            return None

    class Client:
        async def post(self, *_: Any, **__: Any) -> Response:
            return Response()

    class Breaker:
        async def call_async(self, func: Any, *args: Any, **kwargs: Any) -> object:
            return await func(*args, **kwargs)

    result = await post_json_to_remote(
        "https://open.feishu.cn/open-apis/bot/v2/hook/unit",
        {"msg_type": "interactive"},
        policy=ForwardDeliveryPolicy(
            timeout_seconds=2,
            max_attempts=3,
            retry_initial_delay=1,
            retry_max_delay=10,
            retry_backoff_multiplier=2.0,
            stale_processing_threshold_seconds=60,
            max_delivery_age_seconds=1800,
        ),
        dependencies=RemoteForwardDependencies(
            http_client=Client(),
            circuit_breaker=cast(Any, Breaker()),
            validate_url=accept_url,
        ),
        target_type_label="feishu",
    )

    assert result["status"] == "failed"
    assert result["status_code"] == 200
    assert result["error_code"] == "19001"
    assert result["retryable"] is False
    assert result["disable_rule"] is True


def test_feishu_rate_limit_remains_retryable() -> None:
    import httpx

    from services.forwarding.remote import _feishu_business_failure

    response = httpx.Response(
        200,
        json={"StatusCode": 11232, "StatusMessage": "frequency limited"},
        request=httpx.Request("POST", "https://open.feishu.cn/open-apis/bot/v2/hook/unit"),
    )

    failure = _feishu_business_failure(response)

    assert failure is not None
    assert failure["error_code"] == "11232"
    assert failure["retryable"] is True
    assert failure["disable_rule"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "retryable", "disable_rule"),
    [
        (400, False, False),
        (401, False, True),
        (503, True, False),
    ],
)
async def test_http_failures_separate_delivery_retry_from_rule_health(
    status_code: int,
    retryable: bool,
    disable_rule: bool,
) -> None:
    import httpx

    from services.forwarding.circuit_breakers import RemoteForwardDependencies
    from services.forwarding.policies import ForwardDeliveryPolicy
    from services.forwarding.remote import post_json_to_remote

    async def accept_url(url: str, **_: Any) -> str:
        return url

    class Client:
        async def post(self, url: str, **_: Any) -> httpx.Response:
            return httpx.Response(status_code, request=httpx.Request("POST", url))

    class Breaker:
        async def call_async(self, func: Any, *args: Any, **kwargs: Any) -> object:
            return await func(*args, **kwargs)

    result = await post_json_to_remote(
        "https://example.test/hook",
        {"hello": "world"},
        policy=ForwardDeliveryPolicy(
            timeout_seconds=2,
            max_attempts=3,
            retry_initial_delay=1,
            retry_max_delay=10,
            retry_backoff_multiplier=2.0,
            stale_processing_threshold_seconds=60,
            max_delivery_age_seconds=1800,
        ),
        dependencies=RemoteForwardDependencies(
            http_client=Client(),
            circuit_breaker=cast(Any, Breaker()),
            validate_url=accept_url,
        ),
    )

    assert result["status"] == "failed"
    assert result["retryable"] is retryable
    assert result["disable_rule"] is disable_rule


@pytest.mark.asyncio
async def test_post_json_to_remote_sends_idempotency_key_header() -> None:
    """An at-least-once forward must put the outbox idempotency key on the wire
    as an Idempotency-Key header so a cooperating downstream can dedupe."""
    from services.forwarding.circuit_breakers import RemoteForwardDependencies
    from services.forwarding.policies import ForwardDeliveryPolicy
    from services.forwarding.remote import post_json_to_remote

    async def accept_url(url: str, **_kwargs: Any) -> str:
        return url

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self) -> None:
            self.headers_seen: list[dict[str, str] | None] = []

        async def post(self, _url: str, **kwargs: Any) -> Response:
            self.headers_seen.append(kwargs.get("headers"))
            return Response()

    class Breaker:
        async def call_async(self, func: Any, *args: Any, **kwargs: Any) -> object:
            return await func(*args, **kwargs)

    def _deps(client: Client) -> RemoteForwardDependencies:
        return RemoteForwardDependencies(
            http_client=cast(Any, client),
            circuit_breaker=cast(Any, Breaker()),
            validate_url=accept_url,
        )

    policy = ForwardDeliveryPolicy.from_config()

    with_key = Client()
    await post_json_to_remote(
        "https://remote.test/hook",
        {"x": 1},
        policy=policy,
        dependencies=_deps(with_key),
        idempotency_key="forward:42:abc",
    )
    assert with_key.headers_seen == [{"Idempotency-Key": "forward:42:abc"}]

    # No key supplied -> no headers forced (None), so we don't override defaults.
    without_key = Client()
    await post_json_to_remote(
        "https://remote.test/hook",
        {"x": 1},
        policy=policy,
        dependencies=_deps(without_key),
    )
    assert without_key.headers_seen == [None]


@pytest.mark.asyncio
async def test_ingress_backpressure_suppresses_after_threshold() -> None:
    from services.webhooks.ingress_backpressure import check_ingress_backpressure
    from services.webhooks.policies import IngressPolicy

    calls = 0

    async def fake_eval(*_: object) -> int:
        nonlocal calls
        calls += 1
        return calls

    policy = IngressPolicy(
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
    from services.webhooks.policies import IngressPolicy

    async def failing_eval(*_: object) -> int:
        raise RuntimeError("redis unavailable")

    result = await check_ingress_backpressure(
        source_hint="prometheus",
        raw_body=b'{"alertname":"HighCPU","instance":"a"}',
        policy=IngressPolicy(
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
    from api.v1 import webhook
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
