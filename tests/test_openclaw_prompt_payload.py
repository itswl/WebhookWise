from unittest.mock import AsyncMock, MagicMock

import pytest


def test_openclaw_prompt_payload_keeps_full_payload_when_payload_is_large() -> None:
    from services.forwarding.forward import _build_openclaw_prompt_payload

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
async def test_analyze_with_openclaw_sends_utf8_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Config
    from services.forwarding import forward

    monkeypatch.setattr(Config.openclaw, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(Config.openclaw, "OPENCLAW_GATEWAY_URL", "http://openclaw.test")
    monkeypatch.setattr(Config.openclaw, "OPENCLAW_GATEWAY_TOKEN", "token")
    monkeypatch.setattr(Config.openclaw, "OPENCLAW_HOOKS_TOKEN", "")
    monkeypatch.setattr(Config.ai, "DEEP_ANALYSIS_PLATFORM", "openclaw")

    response = MagicMock()
    response.json.return_value = {"runId": "run-1"}
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    async def _call_direct(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    monkeypatch.setattr(forward, "get_http_client", lambda: client)
    monkeypatch.setattr(forward.openclaw_cb, "call_async", _call_direct)

    result = await forward.analyze_with_openclaw(
        {"source": "prometheus", "parsed_data": {"summary": "中文告警", "token": "secret-token"}}
    )

    assert result["_openclaw_run_id"] == "run-1"
    post_kwargs = client.post.await_args.kwargs
    body = post_kwargs["content"]
    assert "json" not in post_kwargs
    assert isinstance(body, bytes)
    assert "中文告警".encode() in body
    assert b"\\u4e2d\\u6587" not in body
    assert b"[REDACTED]" in body
