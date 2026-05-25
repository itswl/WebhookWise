from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import json


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
    from services.analysis.ai_prompt import resolve_prompt_path

    root = Path(__file__).resolve().parents[1]
    path = resolve_prompt_path("prompts/webhook_analysis_detailed.txt")

    assert path == root / "prompts/webhook_analysis_detailed.txt"
    assert path.exists()


@pytest.mark.asyncio
async def test_deep_analysis_prompt_uses_shared_loader(tmp_path: Path) -> None:
    from services.analysis.ai_prompt import (
        DEEP_ANALYSIS_PROMPT_KIND,
        get_prompt_source,
        reload_deep_analysis_prompt_template,
    )
    from services.analysis.analysis_policies import PromptPolicy

    prompt_file = tmp_path / "deep_analysis_prompt.txt"
    prompt_file.write_text("managed deep analysis prompt", encoding="utf-8")

    try:
        template = await reload_deep_analysis_prompt_template(
            PromptPolicy(
                inline_prompt="",
                prompt_file=str(prompt_file),
                builtin_prompt="",
                inline_source="",
                builtin_source="",
            )
        )

        assert template == "managed deep analysis prompt"
        assert get_prompt_source(DEEP_ANALYSIS_PROMPT_KIND) == f"file:{prompt_file}"
    finally:
        await reload_deep_analysis_prompt_template()


def test_sanitize_for_ai_redacts_sensitive_nested_fields(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    from core.sensitive_data import REDACTED
    from services.webhooks.payload_sanitizer import sanitize_for_ai

    monkeypatch.setattr(temp_config.ai, "AI_PAYLOAD_STRIP_KEYS", "")
    cleaned = sanitize_for_ai(
        {
            "service": "checkout",
            "nested": {"token": "secret-token", "safe": "ok"},
            "items": [{"password": "pw"}, {"value": 1}],
        }
    )

    assert cleaned["nested"]["token"] == REDACTED
    assert cleaned["nested"]["safe"] == "ok"
    assert cleaned["items"][0]["password"] == REDACTED


@pytest.mark.asyncio
async def test_forward_to_remote_rejects_private_target() -> None:
    from services.forwarding.remote import forward_to_remote

    result = await forward_to_remote(
        {"source": "test", "parsed_data": {}},
        {"summary": "ok"},
        target_url="http://127.0.0.1:8000/hook",
    )

    assert result["status"] == "invalid_target"


@pytest.mark.asyncio
async def test_request_body_limit_middleware_rejects_oversized_body(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    import httpx

    from core.app import app

    monkeypatch.setattr(temp_config.security, "MAX_WEBHOOK_BODY_BYTES", 4)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/webhook/prometheus", content=b"12345")

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_security_headers_include_hsts() -> None:
    import httpx

    from core.app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get("/ready")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.asyncio
async def test_forward_success_accepts_non_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.forwarding.circuit_breakers import RemoteForwardDependencies
    from services.forwarding.policies import ForwardDeliveryPolicy
    from services.forwarding.remote import forward_to_remote

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

    async def accept_url(url: str) -> str:
        return url

    class Breaker:
        async def call_async(self, fn: Any, *args: Any, **kwargs: Any) -> object:
            return await fn(*args, **kwargs)

    result = await forward_to_remote(
        {"source": "test", "parsed_data": {}},
        {"summary": "ok"},
        target_url="https://example.com/hook",
        policy=ForwardDeliveryPolicy(
            timeout_seconds=2,
            max_attempts=3,
            retry_initial_delay=1,
            retry_max_delay=10,
            retry_backoff_multiplier=2.0,
            stale_processing_threshold_seconds=60,
            max_delivery_age_seconds=1800,
        ),
        dependencies=RemoteForwardDependencies(FakeHttpClient(), Breaker(), accept_url),
    )

    assert result["status"] == "success"
    assert result.get("status_code") == 200


@pytest.mark.asyncio
async def test_forward_revalidates_target_immediately_before_post() -> None:
    from core.url_security import UnsafeTargetUrlError
    from services.forwarding.circuit_breakers import RemoteForwardDependencies
    from services.forwarding.policies import ForwardDeliveryPolicy
    from services.forwarding.remote import forward_to_remote

    validate_calls = 0
    posted_urls: list[str] = []

    async def validate_url(url: str) -> str:
        nonlocal validate_calls
        validate_calls += 1
        if validate_calls == 2:
            raise UnsafeTargetUrlError("target host resolves to a non-public IP")
        return url

    class Client:
        async def post(self, url: str, **_: Any) -> object:
            posted_urls.append(url)
            raise AssertionError("post should not be called after final URL validation fails")

    class Breaker:
        async def call_async(self, fn: Any, *args: Any, **kwargs: Any) -> object:
            return await fn(*args, **kwargs)

    result = await forward_to_remote(
        {"source": "test", "parsed_data": {}},
        {"summary": "ok"},
        target_url="https://example.com/hook",
        policy=ForwardDeliveryPolicy(
            timeout_seconds=2,
            max_attempts=3,
            retry_initial_delay=1,
            retry_max_delay=10,
            retry_backoff_multiplier=2.0,
            stale_processing_threshold_seconds=60,
            max_delivery_age_seconds=1800,
        ),
        dependencies=RemoteForwardDependencies(Client(), Breaker(), validate_url),
    )

    assert result["status"] == "invalid_target"
    assert validate_calls == 2
    assert posted_urls == []


def test_deep_analysis_view_does_not_render_unsanitized_marked_html() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "templates/static/js/deep-analyses.js").read_text()
    html = (root / "templates/dashboard.html").read_text()

    assert "marked.parse" not in js
    assert "marked.min.js" not in html


@pytest.mark.asyncio
async def test_lifespan_rejects_placeholder_admin_write_key(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    from core.app import app, lifespan

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(temp_config.security, "API_KEY", "real-api-key")
    monkeypatch.setattr(temp_config.security, "ADMIN_WRITE_KEY", "please-change-admin-write-key")
    monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "real-webhook-secret")
    monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", True)

    with pytest.raises(RuntimeError, match="ADMIN_WRITE_KEY"):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_manual_forward_requires_target_url_field(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import reanalysis

    event = SimpleNamespace(
        id=1, source="test", ai_analysis={"summary": "ok"}, forward_status=None, importance="high", is_duplicate=False
    )

    class FakeSession:
        committed = False

        async def get(self, model: object, item_id: int) -> object:
            return event

        async def commit(self) -> None:
            self.committed = True

    captured: dict[str, object] = {}

    async def fake_context(item: object) -> dict[str, object]:
        return {"source": "test", "parsed_data": {}}

    async def fake_forward(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "success", "outbox_id": 1}

    fake_session = FakeSession()
    monkeypatch.setattr(reanalysis, "build_webhook_context", fake_context)
    monkeypatch.setattr(reanalysis, "forward_notification", fake_forward)

    # URL validation rejects example.com — bypass for test
    async def _pass_through(url: str, **kw: object) -> str:
        return url

    monkeypatch.setattr(reanalysis, "validate_outbound_url", _pass_through)

    result = await reanalysis.manual_forward_webhook(
        1,
        {"target_url": "https://example.com/hook"},
        session=fake_session,  # type: ignore[arg-type]
    )

    assert result["success"] is True
    assert captured.get("event_type") == "manual_forward"
    assert captured.get("webhook_id") == 1
    assert event.forward_status == "success"
    assert fake_session.committed is True


def test_dashboard_deep_analysis_fields_are_escaped() -> None:
    root = Path(__file__).resolve().parents[1]
    alerts_js = (root / "templates/static/js/alerts.js").read_text()

    assert "record.user_question;" not in alerts_js
    assert "' + record.openclaw_run_id + '" not in alerts_js
    assert "' + analysis.runId + '" not in alerts_js
    assert "Run ID: ${runId}" not in alerts_js


def test_is_feishu_url_requires_hostname_match() -> None:
    from services.notifications.feishu import is_feishu_url

    assert is_feishu_url("https://open.feishu.cn/open-apis/bot/v2/hook/token")
    assert is_feishu_url("https://tenant.larksuite.com/hook")
    assert not is_feishu_url("https://feishu.cn.evil.example/hook")
    assert not is_feishu_url("https://example.com/?next=feishu.cn")


def test_source_hint_is_bounded() -> None:
    from fastapi import HTTPException

    from api.webhook import _normalize_source_hint

    assert _normalize_source_hint(" prometheus ") == "prometheus"
    with pytest.raises(HTTPException):
        _normalize_source_hint("x" * 101)


@pytest.mark.asyncio
async def test_admin_write_key_is_accepted_by_router_and_required_for_write(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    import httpx

    from core.app import app

    monkeypatch.setattr(temp_config.security, "API_KEY", "api-key")
    monkeypatch.setattr(temp_config.security, "ADMIN_WRITE_KEY", "admin-key")

    async def fake_reload() -> str:
        return "test prompt"

    monkeypatch.setattr("api.admin.reload_user_prompt_template", fake_reload)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        read_with_admin = await client.get("/api/prompt", headers={"Authorization": "Bearer admin-key"})
        write_with_api = await client.post("/api/prompt/reload", headers={"Authorization": "Bearer api-key"})
        write_with_admin = await client.post("/api/prompt/reload", headers={"Authorization": "Bearer admin-key"})

    assert read_with_admin.status_code == 200
    assert write_with_api.status_code == 403
    assert write_with_api.json()["detail"] == "Admin write permission required"
    assert write_with_admin.status_code == 200
    assert write_with_admin.json()["template_length"] == len("test prompt")


def test_dashboard_keeps_read_and_write_tokens_separate() -> None:
    root = Path(__file__).resolve().parents[1]
    api_js = (root / "templates/static/js/api.js").read_text()
    dashboard_html = (root / "templates/dashboard.html").read_text()

    assert "const READ_TOKEN_KEY = 'webhook_api_key';" in api_js
    assert "const WRITE_TOKEN_KEY = 'webhook_admin_write_key';" in api_js
    assert "method === 'GET' || method === 'HEAD' ? 'read' : 'write'" in api_js
    assert "this.getWriteToken()" in api_js
    assert "Admin write permission required" in api_js

    assert 'id="authModal"' in dashboard_html
    assert 'id="authApiKey"' in dashboard_html
    assert 'id="authAdminWriteKey"' in dashboard_html


@pytest.mark.asyncio
async def test_webhook_auth_respects_require_webhook_auth_switch(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    import httpx

    from core.app import app
    from services.operations.tasks import process_webhook_task

    monkeypatch.setattr(temp_config.security, "WEBHOOK_RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(temp_config.security, "WEBHOOK_RATE_LIMIT_BURST", 0)
    monkeypatch.setattr(temp_config.security, "WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE", 0)

    enqueued: list[str] = []

    async def fake_kiq(**kwargs: object) -> None:
        enqueued.append(str(kwargs.get("request_id") or ""))

    monkeypatch.setattr(process_webhook_task, "kiq", fake_kiq)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", False)
        monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "configured-but-disabled")
        disabled = await client.post("/webhook/prometheus", json={"alertname": "no-auth"})

        monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", True)
        monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "")
        missing_secret = await client.post("/webhook/prometheus", json={"alertname": "missing-secret"})

        monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "real-secret")
        missing_token = await client.post("/webhook/prometheus", json={"alertname": "missing-token"})
        valid_token = await client.post(
            "/webhook/prometheus",
            json={"alertname": "valid-token"},
            headers={"token": "real-secret"},
        )

    assert disabled.status_code == 202
    assert missing_secret.status_code == 401
    assert missing_token.status_code == 401
    assert valid_token.status_code == 202
    assert len(enqueued) == 2


@pytest.mark.asyncio
async def test_webhook_receive_always_uses_ingress_backpressure_and_taskiq(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    import httpx

    from core.app import app

    monkeypatch.setattr(temp_config.server, "RUN_MODE", "api")
    monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", False)
    monkeypatch.setattr(temp_config.security, "WEBHOOK_RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(temp_config.security, "WEBHOOK_RATE_LIMIT_BURST", 0)
    monkeypatch.setattr(temp_config.security, "WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE", 0)

    backpressure_calls: list[dict[str, object]] = []

    async def fake_backpressure(**kwargs: object) -> object:
        backpressure_calls.append(kwargs)
        return SimpleNamespace(suppressed=False)

    enqueued: list[dict[str, object]] = []

    async def fake_kiq(**kwargs: object) -> None:
        enqueued.append(kwargs)

    monkeypatch.setattr("api.webhook.check_ingress_backpressure", fake_backpressure)
    monkeypatch.setattr("api.webhook.process_webhook_task.kiq", fake_kiq)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/webhook/prometheus", json={"alertname": "canonical"})

    assert response.status_code == 202
    assert response.json()["message"] == "Webhook received and queued for processing"
    assert len(backpressure_calls) == 1
    assert len(enqueued) == 1
    assert enqueued[0]["source_name"] == "prometheus"
    assert json.loads(str(enqueued[0]["raw_body"])) == {"alertname": "canonical"}


@pytest.mark.asyncio
async def test_readiness_requires_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.webhook import readiness_check

    async def db_ok() -> bool:
        return True

    async def redis_failed() -> bool:
        return False

    monkeypatch.setattr("api.webhook.test_db_connection", db_ok)
    monkeypatch.setattr("api.webhook.redis_ping", redis_failed)

    response = await readiness_check()
    body = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 503
    assert body["data"]["redis"] == "failed"
    assert body["data"]["queue"] == "redis_stream"
