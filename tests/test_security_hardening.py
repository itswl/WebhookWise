from pathlib import Path
from typing import Any

import pytest


def test_redacts_headers_and_nested_payload_fields() -> None:
    from core.sensitive_data import REDACTED, redact_event_dict

    event = {
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer secret",
            "X-Webhook-Signature": "sig",
        },
        "raw_payload": '{"service":"api","token":"abc","nested":{"password":"pw","value":1}}',
        "parsed_data": {"api_key": "k", "safe": "value"},
    }

    redacted = redact_event_dict(event)

    assert redacted["headers"]["Content-Type"] == "application/json"
    assert redacted["headers"]["Authorization"] == REDACTED
    assert redacted["headers"]["X-Webhook-Signature"] == REDACTED
    assert '"token":"[REDACTED]"' in redacted["raw_payload"]
    assert '"password":"[REDACTED]"' in redacted["raw_payload"]
    assert redacted["parsed_data"]["api_key"] == REDACTED
    assert redacted["parsed_data"]["safe"] == "value"


def test_non_json_raw_payload_is_not_echoed() -> None:
    from core.sensitive_data import redact_raw_payload_text

    redacted = redact_raw_payload_text("token=abc123&message=hello")

    assert redacted is not None
    assert "token=abc123" not in redacted
    assert redacted.startswith("[REDACTED_NON_JSON_PAYLOAD")


def test_default_prompt_path_resolves_from_project_root() -> None:
    from services.analysis.ai_analyzer import _resolve_prompt_path

    root = Path(__file__).resolve().parents[1]
    path = _resolve_prompt_path("prompts/webhook_analysis_detailed.txt")

    assert path == root / "prompts/webhook_analysis_detailed.txt"
    assert path.exists()


@pytest.mark.asyncio
async def test_forward_success_accepts_non_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.forwarding import forward as forward_mod

    class FakeResponse:
        status_code = 200
        content = b"ok"
        text = "ok"

        def json(self) -> dict[str, Any]:
            raise ValueError("not json")

        def raise_for_status(self) -> None:
            return None

    class FakeHttpClient:
        async def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
            return FakeResponse()

    async def call_async(fn: Any) -> Any:
        return await fn()

    monkeypatch.setattr(forward_mod, "get_http_client", lambda: FakeHttpClient())
    monkeypatch.setattr(forward_mod.forward_cb, "call_async", call_async)

    result = await forward_mod.forward_to_remote(
        {"source": "test", "parsed_data": {}},
        {"summary": "ok"},
        target_url="https://example.com/hook",
    )

    assert result["status"] == "success"
    assert result["response"] == {"_raw": "ok"}


def test_deep_analysis_view_does_not_render_unsanitized_marked_html() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "templates/static/js/deep-analyses.js").read_text()
    html = (root / "templates/dashboard.html").read_text()

    assert "marked.parse" not in js
    assert "marked.min.js" not in html
