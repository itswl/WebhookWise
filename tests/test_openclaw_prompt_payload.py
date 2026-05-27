from unittest.mock import AsyncMock, MagicMock

import pytest


def test_openclaw_prompt_payload_keeps_full_payload_when_payload_is_large() -> None:
    from services.analysis.openclaw import _build_openclaw_prompt_payload

    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "4fe6502e-587e-43a1-860f-bb575ab8476b",
                    "internal_label_alert_id": "6a0142e48f78951ec14b1fa4",
                    "internal_label_namespace": "eve-cn-prod",
                    "internal_label_service": "ai-router",
                },
                "annotations": {"summary": "OpenRouter success rate is 0%"},
                "startsAt": "2026-05-11T02:51:00Z",
            }
        ],
        "raw_debug_blob": "x" * 100_000,
    }

    result = _build_openclaw_prompt_payload("prometheus", payload)

    assert result["overview"]["labels"]["internal_label_service"] == "ai-router"
    assert result["overview"]["annotations"]["summary"] == "OpenRouter success rate is 0%"
    assert result["payload"]["raw_debug_blob"] == "x" * 100_000
    assert "payload_note" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "response_payload", "expected_url"),
    [
        ("openclaw", {"runId": "run-1"}, "http://openclaw.test/hooks/agent"),
        ("hermes", {"delivery_id": "run-1"}, "http://openclaw.test/webhooks/agent"),
    ],
)
async def test_analyze_with_openclaw_sends_utf8_json_body(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    response_payload: dict[str, str],
    expected_url: str,
) -> None:
    from services.analysis.openclaw import analyze_with_openclaw
    from services.forwarding.circuit_breakers import OpenClawForwardDependencies
    from services.forwarding.policies import OpenClawTriggerPolicy

    async def fake_load_prompt() -> str:
        return "managed deep-analysis prompt"

    response = MagicMock()
    response.json.return_value = response_payload
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    class Breaker:
        async def call_async(self, fn, *args, **kwargs):
            return await fn(*args, **kwargs)

    monkeypatch.setattr("services.analysis.openclaw.load_deep_analysis_prompt_template", fake_load_prompt)

    result = await analyze_with_openclaw(
        {"source": "prometheus", "parsed_data": {"summary": "中文告警", "token": "secret-token"}},
        policy=OpenClawTriggerPolicy(
            enabled=True,
            timeout_seconds=900,
            platform=platform,
            gateway_url="http://openclaw.test",
            hooks_token="token",
            connect_timeout=13.0,
            enable_degradation=False,
        ),
        dependencies=OpenClawForwardDependencies(client, Breaker()),
    )

    assert result["_openclaw_run_id"] == "run-1"
    post_args = client.post.await_args.args
    post_kwargs = client.post.await_args.kwargs
    body = post_kwargs["content"]
    headers = post_kwargs["headers"]
    timeout = post_kwargs["timeout"]
    assert post_args[0] == expected_url
    assert "json" not in post_kwargs
    assert isinstance(body, bytes)
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert b"managed deep-analysis prompt" in body
    assert "中文告警".encode() in body
    assert b"\\u4e2d\\u6587" not in body
    assert b"[REDACTED]" in body
    assert timeout.connect == 13.0
    assert timeout.read == 900
