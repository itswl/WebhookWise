import json as pyjson
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import json
from tests.helpers.paths import PROJECT_ROOT


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

    path = resolve_prompt_path("prompts/webhook_analysis_detailed.txt")

    assert path == PROJECT_ROOT / "prompts/webhook_analysis_detailed.txt"
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

    from api.app import app

    monkeypatch.setattr(temp_config.security, "MAX_WEBHOOK_BODY_BYTES", 4)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/v1/webhook/prometheus", content=b"12345")

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_security_headers_include_hsts() -> None:
    import httpx

    from api.app import app

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

    async def validate_url(url: str, **kwargs: Any) -> str:
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
    js = (PROJECT_ROOT / "templates/static/js/deep-analyses.js").read_text()
    html = (PROJECT_ROOT / "templates/dashboard.html").read_text()

    assert "marked.parse" not in js
    assert "marked.min.js" not in html


@pytest.mark.asyncio
async def test_lifespan_rejects_placeholder_admin_write_key(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    from api.app import app, lifespan

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
    from api.v1 import reanalysis

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
    async def _pass_through(url: str, **_kw: object) -> str:
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


@pytest.mark.asyncio
async def test_manual_forward_failure_does_not_leak_downstream_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import DELIVERY_ERROR_MESSAGE
    from api.v1 import reanalysis

    event = SimpleNamespace(
        id=1, source="test", ai_analysis={"summary": "ok"}, forward_status=None, importance="high", is_duplicate=False
    )

    class FakeSession:
        async def get(self, _model: object, _item_id: int) -> object:
            return event

        async def commit(self) -> None:
            return None

    async def fake_context(_item: object) -> dict[str, object]:
        return {"source": "test", "parsed_data": {}}

    async def fake_forward(**_kwargs: object) -> dict[str, object]:
        return {"status": "failed", "message": "postgresql://user:pass@db.internal/webhooks"}

    async def _pass_through(url: str, **kw: object) -> str:
        return url

    monkeypatch.setattr(reanalysis, "build_webhook_context", fake_context)
    monkeypatch.setattr(reanalysis, "forward_notification", fake_forward)
    monkeypatch.setattr(reanalysis, "validate_outbound_url", _pass_through)

    response = await reanalysis.manual_forward_webhook(
        1,
        {"target_url": "https://example.com/hook"},
        session=FakeSession(),  # type: ignore[arg-type]
    )

    body = json.loads(response.body)
    assert response.status_code == 502
    assert body["error"] == DELIVERY_ERROR_MESSAGE
    assert "postgresql://" not in response.body.decode()


@pytest.mark.asyncio
async def test_manual_forward_invalid_target_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import TARGET_URL_UNAVAILABLE_MESSAGE
    from api.v1 import reanalysis
    from core.url_security import UnsafeTargetUrlError

    event = SimpleNamespace(
        id=1, source="test", ai_analysis={"summary": "ok"}, forward_status=None, importance="high", is_duplicate=False
    )

    class FakeSession:
        async def get(self, _model: object, _item_id: int) -> object:
            return event

    async def reject_url(_url: str) -> str:
        raise UnsafeTargetUrlError("target host resolves to a non-public IP")

    monkeypatch.setattr(reanalysis, "validate_outbound_url", reject_url)

    response = await reanalysis.manual_forward_webhook(
        1,
        {"target_url": "https://example.com/hook"},
        session=FakeSession(),  # type: ignore[arg-type]
    )

    body = json.loads(response.body)
    assert response.status_code == 400
    assert body["error"] == TARGET_URL_UNAVAILABLE_MESSAGE
    assert "non-public IP" not in response.body.decode()


@pytest.mark.asyncio
async def test_reanalysis_unhandled_exception_is_sanitized() -> None:
    from api import INTERNAL_ERROR_MESSAGE
    from api.v1 import reanalysis

    class FakeSession:
        async def get(self, _model: object, _item_id: int) -> object:
            raise RuntimeError("postgresql://user:pass@db.internal/webhooks")

    response = await reanalysis.reanalyze_webhook(1, session=FakeSession())  # type: ignore[arg-type]

    assert response.status_code == 500
    assert INTERNAL_ERROR_MESSAGE in response.body.decode()
    assert "postgresql://" not in response.body.decode()


@pytest.mark.asyncio
async def test_global_exception_handler_is_sanitized() -> None:
    from api import INTERNAL_ERROR_MESSAGE
    from api.app import unhandled_exception_handler

    request = SimpleNamespace(url=SimpleNamespace(path="/v1/leaky"))
    response = await unhandled_exception_handler(  # type: ignore[arg-type]
        request,
        RuntimeError("postgresql://user:pass@db.internal/webhooks"),
    )

    assert response.status_code == 500
    assert INTERNAL_ERROR_MESSAGE in response.body.decode()
    assert "postgresql://" not in response.body.decode()


@pytest.mark.asyncio
async def test_global_unhandled_exception_handler_is_sanitized() -> None:
    from api import INTERNAL_ERROR_MESSAGE
    from api.app import app, unhandled_exception_handler

    assert app.debug is False
    request = SimpleNamespace(url=SimpleNamespace(path="/v1/leaky"))
    response = await unhandled_exception_handler(
        request,  # type: ignore[arg-type]
        RuntimeError("Traceback postgresql://user:pass@db.internal/webhooks"),
    )

    body = response.body.decode()
    assert response.status_code == 500
    assert INTERNAL_ERROR_MESSAGE in body
    assert "Traceback" not in body
    assert "postgresql://" not in body


@pytest.mark.asyncio
async def test_forward_rule_test_exception_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import DELIVERY_ERROR_MESSAGE
    from api.v1 import forwarding

    rule = SimpleNamespace(id=1, name="leaky", target_type="webhook", target_url="https://example.com/hook")

    async def fake_get_rule(_session: object, _rule_id: int) -> object:
        return rule

    async def fake_post(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("postgresql://user:pass@db.internal/webhooks")

    monkeypatch.setattr(forwarding, "get_forward_rule", fake_get_rule)
    monkeypatch.setattr("services.forwarding.remote.post_json_to_remote", fake_post)

    response = await forwarding.test_forward_rule_endpoint(1, session=object())  # type: ignore[arg-type]
    body = json.loads(response.body)

    assert response.status_code == 502
    assert body["error"] == DELIVERY_ERROR_MESSAGE
    assert "postgresql://" not in response.body.decode()


@pytest.mark.asyncio
async def test_forward_rule_invalid_target_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import TARGET_URL_UNAVAILABLE_MESSAGE
    from api.v1 import forwarding
    from core.url_security import UnsafeTargetUrlError
    from schemas.forwarding import ForwardRuleCreateRequest

    async def reject_target(_target_type: str, _target_url: object) -> str:
        raise UnsafeTargetUrlError("target host is not in FORWARD_TARGET_ALLOWLIST")

    monkeypatch.setattr(forwarding, "_validated_target_url", reject_target)

    response = await forwarding.create_forward_rule_endpoint(
        ForwardRuleCreateRequest(name="blocked", target_type="webhook", target_url="https://example.com/hook"),
        session=object(),  # type: ignore[arg-type]
    )
    body = json.loads(response.body)

    assert response.status_code == 400
    assert body["error"] == TARGET_URL_UNAVAILABLE_MESSAGE
    assert "FORWARD_TARGET_ALLOWLIST" not in response.body.decode()


def test_dashboard_deep_analysis_fields_are_escaped() -> None:
    alerts_js = (PROJECT_ROOT / "templates/static/js/alerts.js").read_text()

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

    from api.v1.webhook import _normalize_source_hint

    assert _normalize_source_hint(" prometheus ") == "prometheus"
    with pytest.raises(HTTPException):
        _normalize_source_hint("x" * 101)


@pytest.mark.asyncio
async def test_admin_write_key_does_not_bypass_api_key_and_requires_mixed_headers(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    import httpx

    from api.app import app

    monkeypatch.setattr(temp_config.security, "API_KEY", "api-key")
    monkeypatch.setattr(temp_config.security, "ADMIN_WRITE_KEY", "admin-key")

    async def fake_reload() -> str:
        return "test prompt"

    monkeypatch.setattr("api.v1.admin.reload_user_prompt_template", fake_reload)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # verify_api_key 只接受 API_KEY，不再接受 ADMIN_WRITE_KEY
        read_with_api = await client.get("/v1/prompt", headers={"Authorization": "Bearer api-key"})
        read_with_admin = await client.get("/v1/prompt", headers={"Authorization": "Bearer admin-key"})
        read_with_admin_header = await client.get("/v1/prompt", headers={"x-admin-write-key": "admin-key"})
        # 写操作需要同时通过 verify_api_key + verify_admin_write
        # 只有 Bearer api-key 无法通过 admin write 检查
        write_with_api = await client.post("/v1/prompt/reload", headers={"Authorization": "Bearer api-key"})
        # 只有 Bearer admin-key 无法通过 verify_api_key 检查
        write_with_admin_bearer = await client.post("/v1/prompt/reload", headers={"Authorization": "Bearer admin-key"})
        # 只有 x-admin-write-key header 无法通过 verify_api_key 检查
        write_with_admin_header = await client.post("/v1/prompt/reload", headers={"x-admin-write-key": "admin-key"})
        # 正确方式：Bearer api-key + x-admin-write-key admin-key
        write_with_mixed_headers = await client.post(
            "/v1/prompt/reload",
            headers={"Authorization": "Bearer api-key", "x-admin-write-key": "admin-key"},
        )

    assert read_with_api.status_code == 200
    assert read_with_admin.status_code == 401
    assert read_with_admin_header.status_code == 401
    assert write_with_api.status_code == 403
    assert write_with_api.json()["detail"] == "Admin write token required. API key is insufficient for this endpoint."
    assert write_with_admin_bearer.status_code == 401
    assert write_with_admin_header.status_code == 401
    assert write_with_mixed_headers.status_code == 200
    assert write_with_mixed_headers.json()["template_length"] == len("test prompt")


def test_dashboard_keeps_read_and_write_tokens_separate() -> None:
    api_js = (PROJECT_ROOT / "templates/static/js/api.js").read_text()
    dashboard_html = (PROJECT_ROOT / "templates/dashboard.html").read_text()

    assert "const READ_TOKEN_KEY = 'webhook_api_key';" in api_js
    assert "const WRITE_TOKEN_KEY = 'webhook_admin_write_key';" in api_js
    assert "method === 'GET' || method === 'HEAD' ? 'read' : 'write'" in api_js
    assert "this.getWriteToken()" in api_js
    assert "Admin write permission required" in api_js
    assert "API key is insufficient for this endpoint" in api_js
    assert "window.indexedDB" in api_js
    assert "localStorage.setItem(storageKey, JSON.stringify(record))" in api_js

    assert 'id="authModal"' in dashboard_html
    assert 'id="authApiKey"' in dashboard_html
    assert 'id="authAdminWriteKey"' in dashboard_html
    assert "Web Crypto 加密后保存在本机 localStorage" in dashboard_html


def test_dashboard_token_storage_encrypts_and_decrypts_with_webcrypto() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for dashboard crypto behavior test")

    api_js = PROJECT_ROOT / "templates/static/js/api.js"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync({pyjson.dumps(str(api_js))}, 'utf8');
const storage = new Map();
const calls = [];

const context = {{
  console: {{ warn() {{}}, error() {{}}, log() {{}} }},
  TextEncoder,
  TextDecoder,
  Uint8Array,
  ArrayBuffer,
  URLSearchParams
}};
context.window = context;
context.localStorage = {{
  getItem(key) {{ return storage.has(key) ? storage.get(key) : null; }},
  setItem(key, value) {{ storage.set(key, value); }},
  removeItem(key) {{ storage.delete(key); }}
}};
context.window.btoa = (binary) => Buffer.from(binary, 'binary').toString('base64');
context.window.atob = (value) => Buffer.from(value, 'base64').toString('binary');
context.window.indexedDB = {{}};
context.window.crypto = {{
  getRandomValues(array) {{
    for (let i = 0; i < array.length; i += 1) array[i] = i + 1;
    return array;
  }},
  subtle: {{
    async encrypt(algorithm, _key, data) {{
      if (algorithm.name !== 'AES-GCM') throw new Error('unexpected encrypt algorithm: ' + algorithm.name);
      if (!(algorithm.iv instanceof Uint8Array) || algorithm.iv.length !== 12) throw new Error('bad encrypt iv');
      calls.push(['encrypt', algorithm.name, algorithm.iv.length]);
      return data;
    }},
    async decrypt(algorithm, _key, data) {{
      if (algorithm.name !== 'AES-GCM') throw new Error('unexpected decrypt algorithm: ' + algorithm.name);
      if (!(algorithm.iv instanceof Uint8Array) || algorithm.iv.length !== 12) throw new Error('bad decrypt iv');
      calls.push(['decrypt', algorithm.name, algorithm.iv.length]);
      return data;
    }}
  }}
}};

vm.runInNewContext(source + '\\nthis.__API = API;', context, {{ filename: 'api.js' }});

(async () => {{
  const api = context.__API;
  api._cryptoKeyPromise = Promise.resolve({{ stub: true }});
  await api.setEncryptedToken('unit-token-key', 'read', 'secret-token');
  const stored = JSON.parse(context.localStorage.getItem('unit-token-key'));
  const restored = await api.loadEncryptedToken('unit-token-key');
  if (stored.alg !== 'AES-GCM') throw new Error('record algorithm was not persisted');
  if (restored !== 'secret-token') throw new Error('decrypted token mismatch');
  if (api.getReadToken() !== 'secret-token') throw new Error('read token cache mismatch');
  if (calls.length !== 2) throw new Error('expected encrypt and decrypt calls');
  console.log(JSON.stringify({{ restored, calls }}));
}})().catch((error) => {{
  console.error(error.stack || error.message);
  process.exit(1);
}});
"""
    subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)


@pytest.mark.asyncio
async def test_webhook_auth_respects_require_webhook_auth_switch(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    import httpx

    from api.app import app
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
        disabled = await client.post("/v1/webhook/prometheus", json={"alertname": "no-auth"})

        monkeypatch.setattr(temp_config.security, "REQUIRE_WEBHOOK_AUTH", True)
        monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "")
        missing_secret = await client.post("/v1/webhook/prometheus", json={"alertname": "missing-secret"})

        monkeypatch.setattr(temp_config.security, "WEBHOOK_SECRET", "real-secret")
        missing_token = await client.post("/v1/webhook/prometheus", json={"alertname": "missing-token"})
        valid_token = await client.post(
            "/v1/webhook/prometheus",
            json={"alertname": "valid-token"},
            headers={"token": "real-secret"},
        )

    assert disabled.status_code == 200
    assert missing_secret.status_code == 401
    assert missing_token.status_code == 401
    assert valid_token.status_code == 200
    assert len(enqueued) == 2


@pytest.mark.asyncio
async def test_webhook_receive_always_uses_ingress_backpressure_and_taskiq(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    import httpx

    from api.app import app

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

    monkeypatch.setattr("api.v1.webhook.check_ingress_backpressure", fake_backpressure)
    monkeypatch.setattr("api.v1.webhook.process_webhook_task.kiq", fake_kiq)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/v1/webhook/prometheus", json={"alertname": "canonical"})

    assert response.status_code == 200
    assert response.json()["message"] == "Webhook received and queued for processing"
    assert len(backpressure_calls) == 1
    assert len(enqueued) == 1
    assert enqueued[0]["source_name"] == "prometheus"
    assert json.loads(str(enqueued[0]["raw_body"])) == {"alertname": "canonical"}


@pytest.mark.asyncio
async def test_readiness_requires_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.health import readiness_check

    async def db_ok() -> bool:
        return True

    async def redis_failed() -> bool:
        return False

    monkeypatch.setattr("api.health.test_db_connection", db_ok)
    monkeypatch.setattr("api.health.redis_ping", redis_failed)

    response = await readiness_check()
    body = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 503
    assert body["data"]["redis"] == "failed"
    assert body["data"]["queue"] == "redis_stream"


def _admin_rl_request() -> Any:
    return SimpleNamespace(
        client=SimpleNamespace(host="9.9.9.9"),
        headers={},
        url=SimpleNamespace(path="/v1/webhooks"),
        app=SimpleNamespace(state=SimpleNamespace()),
    )


@pytest.mark.asyncio
async def test_admin_rate_limit_disabled_is_noop(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    from fastapi import Response

    from core import webhook_security

    monkeypatch.setattr(temp_config.security, "ADMIN_API_RATE_LIMIT_PER_MINUTE", 0)

    called = False

    async def must_not_run(*_a: object, **_k: object) -> int:
        nonlocal called
        called = True
        return 1

    monkeypatch.setattr(webhook_security, "redis_eval_int", must_not_run)
    response = Response()
    # Disabled -> returns without touching Redis or setting headers.
    await webhook_security.check_admin_rate_limit_dep(_admin_rl_request(), response, temp_config)
    assert called is False
    assert "X-RateLimit-Limit" not in response.headers


@pytest.mark.asyncio
async def test_admin_rate_limit_allows_then_rejects(monkeypatch: pytest.MonkeyPatch, temp_config: Any) -> None:
    from fastapi import HTTPException, Response

    from core import webhook_security

    monkeypatch.setattr(temp_config.security, "ADMIN_API_RATE_LIMIT_PER_MINUTE", 5)

    async def redis_ok(_op: str) -> bool:
        return True

    monkeypatch.setattr("core.redis_health.ensure_redis_available", redis_ok)

    # remaining >= 0 -> allowed (headers set, no raise).
    async def remaining_two(*_a: object, **_k: object) -> int:
        return 2

    monkeypatch.setattr(webhook_security, "redis_eval_int", remaining_two)
    response = Response()
    await webhook_security.check_admin_rate_limit_dep(_admin_rl_request(), response, temp_config)
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "2"

    # remaining < 0 -> over limit -> 429 with Retry-After.
    async def over_limit(*_a: object, **_k: object) -> int:
        return -1

    monkeypatch.setattr(webhook_security, "redis_eval_int", over_limit)
    rejected = Response()
    with pytest.raises(HTTPException) as exc:
        await webhook_security.check_admin_rate_limit_dep(_admin_rl_request(), rejected, temp_config)
    assert exc.value.status_code == 429
    assert rejected.headers["X-RateLimit-Remaining"] == "0"
    assert int(rejected.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_admin_rate_limit_fails_open_on_redis_trouble(
    monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    from fastapi import Response

    from core import webhook_security

    monkeypatch.setattr(temp_config.security, "ADMIN_API_RATE_LIMIT_PER_MINUTE", 5)

    # Redis unavailable -> allow (fail-open), no raise.
    async def redis_down(_op: str) -> bool:
        return False

    monkeypatch.setattr("core.redis_health.ensure_redis_available", redis_down)
    await webhook_security.check_admin_rate_limit_dep(_admin_rl_request(), Response(), temp_config)

    # Redis raising -> allow (fail-open), no raise.
    async def redis_ok(_op: str) -> bool:
        return True

    async def boom(*_a: object, **_k: object) -> int:
        from redis.exceptions import RedisError

        raise RedisError("down")

    monkeypatch.setattr("core.redis_health.ensure_redis_available", redis_ok)
    monkeypatch.setattr(webhook_security, "redis_eval_int", boom)
    monkeypatch.setattr("core.redis_health.mark_redis_failure", lambda _op, _e: None)
    # Must not raise.
    await webhook_security.check_admin_rate_limit_dep(_admin_rl_request(), Response(), temp_config)
