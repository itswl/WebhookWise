from __future__ import annotations

import base64
from datetime import timedelta
from typing import Any

import httpx
import pytest

from contracts.webhook_payload import webhook_data_from_mapping
from core import json
from core.circuit_breaker import CircuitBreakerOpenException
from core.datetime_utils import utcnow
from services.forwarding.circuit_breakers import DeepAnalysisForwardDependencies
from services.forwarding.policies import DeepAnalysisTriggerPolicy
from services.webhooks.types import DeepAnalysisStatus, degraded_forward_result
from tests.helpers.metric_helpers import MetricCall, StubMetric


class _FakeWebSocket:
    def __init__(self, frames: list[object] | None = None) -> None:
        self.frames = frames or []
        self.sent: list[str] = []
        self.history_recv_count = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> object:
        if self.frames:
            value = self.frames.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        raise TimeoutError


class _HistoryWebSocket(_FakeWebSocket):
    def __init__(self, response_payload: dict[str, object] | None = None, *, error_message: str | None = None) -> None:
        super().__init__()
        self.response_payload = response_payload or {}
        self.error_message = error_message

    async def recv(self) -> object:
        self.history_recv_count += 1
        if self.history_recv_count == 1:
            return json.dumps({"type": "event", "event": "ignored"})
        request_id = json.loads(self.sent[-1])["id"]
        if self.error_message:
            return json.dumps({"type": "res", "id": request_id, "ok": False, "error": {"message": self.error_message}})
        return json.dumps({"type": "res", "id": request_id, "ok": True, "payload": self.response_payload})


class _ConnectContext:
    def __init__(self, ws: _FakeWebSocket) -> None:
        self.ws = ws

    async def __aenter__(self) -> _FakeWebSocket:
        return self.ws

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _poll_policy(**overrides: object) -> Any:
    from services.analysis.deep_analysis_gateway import DeepAnalysisPollPolicy

    values: dict[str, object] = {
        "timeout_seconds": 60,
        "poll_timeout_seconds": 10,
        "poll_initial_delay_seconds": 5,
        "poll_max_delay_seconds": 30,
        "poll_backoff_multiplier": 2.0,
        "http_api_url": "",
        "gateway_url": "http://gateway.test",
        "gateway_token": "gateway-token",
        "hooks_token": "hooks-token",
        "connect_timeout_seconds": 2.0,
        "stability_required_hits": 2,
        "stability_ttl_seconds": 120,
        "max_consecutive_errors": 2,
        "enable_degradation": True,
        "notification_webhook_url": "https://feishu.test/hook",
    }
    values.update(overrides)
    return DeepAnalysisPollPolicy(**values)


def _trigger_policy(**overrides: object) -> DeepAnalysisTriggerPolicy:
    values: dict[str, object] = {
        "enabled": True,
        "timeout_seconds": 60,
        "platform": "openclaw",
        "gateway_url": "http://gateway.test",
        "hooks_token": "hooks-token",
        "connect_timeout": 2.0,
        "enable_degradation": True,
    }
    values.update(overrides)
    return DeepAnalysisTriggerPolicy(**values)


def _record(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": 7,
        "webhook_event_id": 11,
        "engine": "openclaw",
        "gateway_session_key": "session-1",
        "gateway_run_id": "run-1",
        "created_at": utcnow(),
        "status": DeepAnalysisStatus.PENDING,
        "analysis_result": None,
        "duration_seconds": 0,
        "poll_attempts": 0,
        "last_polled_at": None,
    }
    values.update(overrides)
    return values


class _PollResponse:
    def __init__(self, status_code: int, payload: object = None, *, json_error: Exception | None = None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.json_error = json_error

    def json(self) -> object:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class _PollingClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    async def get(self, url: str, **kwargs: object) -> object:
        self.calls.append({"url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _PostClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, **kwargs: object) -> object:
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _PostResponse:
    status_code = 200

    def __init__(self, payload: object = None, *, raise_error: Exception | None = None) -> None:
        self.payload = payload if payload is not None else {"runId": "run-1"}
        self.raise_error = raise_error

    def raise_for_status(self) -> None:
        if self.raise_error is not None:
            raise self.raise_error

    def json(self) -> object:
        return self.payload


def test_gateway_json_url_and_history_parsing_helpers() -> None:
    from services.analysis import deep_analysis_gateway as gateway

    assert gateway._loads_dict(b'{"ok": true}') == {"ok": True}
    assert gateway._loads_dict(b"\xff") is None
    assert gateway._loads_dict("[1, 2]") is None
    assert gateway._loads_dict({"already": "dict"}) is None
    assert gateway._http_to_ws_url("https://gateway.test/") == "wss://gateway.test/ws"
    assert gateway._http_to_ws_url("http://gateway.test") == "ws://gateway.test/ws"
    assert gateway._http_to_ws_url("gateway.test") == "ws://gateway.test/ws"

    plain_frame = gateway._build_connect_frame("token")
    assert plain_frame["params"]["client"]["mode"] == "backend"
    assert plain_frame["params"]["auth"] == {"token": "token"}

    device_frame = gateway._build_connect_frame(
        "token",
        {
            "role": "operator",
            "scopes": ["operator.read"],
            "device_token": "device-token",
            "device": {"id": "device-1234567890"},
        },
    )
    assert device_frame["params"]["role"] == "operator"
    assert device_frame["params"]["auth"]["deviceToken"] == "device-token"
    assert device_frame["params"]["client"]["mode"] == "cli"

    assert gateway._parse_history_messages([]) == {"status": "pending"}
    assert gateway._parse_history_messages([{"message": {"role": "user", "content": "wait"}}]) == {"status": "pending"}
    assert gateway._parse_history_messages([{"message": {"role": "assistant", "content": [{"type": "tool_use"}]}}]) == {
        "status": "pending"
    }
    completed = gateway._parse_history_messages(
        [{"message": {"role": "assistant", "content": [{"type": "text", "text": "root cause"}]}}]
    )
    assert completed["status"] == "completed"
    assert completed["text"] == "root cause"


def test_gateway_policy_helpers_device_auth_and_overview(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from services.analysis import deep_analysis_gateway, deep_analysis_trigger

    monkey_cfg = temp_config.deep_analysis
    monkeypatch.setattr(monkey_cfg, "DEEP_ANALYSIS_DEVICE_ID", "device-id")
    monkeypatch.setattr(monkey_cfg, "DEEP_ANALYSIS_DEVICE_TOKEN", "device-token")
    monkeypatch.setattr(monkey_cfg, "DEEP_ANALYSIS_GATEWAY_TOKEN", "gateway-token")
    monkeypatch.setattr(monkey_cfg, "DEEP_ANALYSIS_NONCE_TIMEOUT_SECONDS", 4.0)
    monkeypatch.setattr(monkey_cfg, "DEEP_ANALYSIS_WS_MAX_HISTORY_FRAMES", 0)
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(
        monkey_cfg,
        "DEEP_ANALYSIS_DEVICE_PRIVATE_KEY_PEM",
        "".join(line for line in pem.splitlines() if not line.startswith("-----")),
    )

    ws_policy = deep_analysis_gateway.DeepAnalysisWsPolicy.from_config()
    assert ws_policy.max_history_frames == 1

    auth = deep_analysis_gateway._build_device_auth("nonce-1", gateway_token="gateway-token", policy=ws_policy)
    assert auth is not None
    assert auth["device_token"] == "device-token"
    assert auth["device"]["id"] == "device-id"
    assert auth["device"]["nonce"] == "nonce-1"
    base64.urlsafe_b64decode(auth["device"]["signature"] + "==")

    no_auth = deep_analysis_gateway._build_device_auth(
        "nonce",
        policy=deep_analysis_gateway.DeepAnalysisWsPolicy("", "", "", "gateway", 1.0, 1),
    )
    assert no_auth is None
    invalid_auth = deep_analysis_gateway._build_device_auth(
        "nonce",
        policy=deep_analysis_gateway.DeepAnalysisWsPolicy("device", "not-base64", "", "gateway", 1.0, 1),
    )
    assert invalid_auth is None

    overview = deep_analysis_trigger._extract_gateway_overview(
        "prometheus",
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "HighCPU",
                        "severity": "critical",
                        "internal_label_alert_id": "fp-1",
                    },
                    "annotations": {"description": "cpu high"},
                    "startsAt": "2026-05-27T00:00:00Z",
                }
            ]
        },
    )
    assert overview["rule_name"] == "HighCPU"
    assert overview["prometheus_alert"]["fingerprint"] == "fp-1"
    assert deep_analysis_trigger._dict_or_empty([("not", "dict")]) == {}


@pytest.mark.asyncio
async def test_poll_openclaw_final_status_matrix() -> None:
    from services.analysis import deep_analysis_gateway as gateway

    policy = _poll_policy(http_api_url="http://gateway.test", poll_timeout_seconds=7, connect_timeout_seconds=3)

    client = _PollingClient(
        [
            _PollResponse(404),
            _PollResponse(202),
            _PollResponse(500),
        ]
    )
    error = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=client,
        retry_count=3,
        trace_id="trace-id",
    )
    assert error == {"status": "error", "error": "HTTP 500"}
    first_call = client.calls[0]
    assert first_call["url"] == "http://gateway.test/sessions/session-1/final"
    assert first_call["headers"]["X-Trace-Id"] == "trace-id"

    invalid_json = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([_PollResponse(200, json_error=ValueError("bad json"))]),
        retry_count=1,
    )
    assert invalid_json == {"status": "error", "error": "Invalid JSON response"}

    invalid_object = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([_PollResponse(200, ["not", "dict"])]),
        retry_count=1,
    )
    assert invalid_object == {"status": "error", "error": "Invalid JSON response"}

    pending_processing = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([_PollResponse(200, {"isProcessing": True})]),
        retry_count=1,
    )
    assert pending_processing == {"status": "pending"}

    pending_not_final = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([_PollResponse(200, {"isFinal": False, "text": "partial"})]),
        retry_count=1,
    )
    assert pending_not_final == {"status": "pending"}

    completed = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([_PollResponse(200, {"isFinal": True, "text": "done", "messageCount": 4})]),
        retry_count=1,
    )
    assert completed == {"status": "completed", "text": "done", "msg_count": 4, "is_final": True}

    no_text = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([_PollResponse(200, {"isFinal": True})]),
        retry_count=1,
    )
    assert no_text == {"status": "error", "error": "No text content"}

    read_timeout = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([httpx.ReadTimeout("read")]),
        retry_count=1,
    )
    assert read_timeout == {"status": "pending", "error": "ReadTimeout after 7s"}

    transport = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([httpx.ConnectTimeout("connect")]),
        retry_count=1,
    )
    assert transport["status"] == "error"
    assert transport["retryable"] is True

    generic = await gateway.poll_gateway_final(
        "session-1",
        policy=policy,
        http_client=_PollingClient([RuntimeError()]),
        retry_count=1,
    )
    assert generic["status"] == "error"
    assert generic["retryable"] is True
    assert "RuntimeError" in str(generic["error"])


@pytest.mark.asyncio
async def test_gateway_challenge_and_handshake_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.analysis import deep_analysis_gateway as gateway

    class WsPolicy:
        nonce_timeout = 1
        max_history_frames = 3

    challenge_ws = _FakeWebSocket(
        [json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce-1"}})]
    )
    assert await gateway._try_recv_challenge(challenge_ws, policy=WsPolicy()) == "nonce-1"
    assert await gateway._try_recv_challenge(_FakeWebSocket([TimeoutError()]), policy=WsPolicy()) is None
    assert await gateway._try_recv_challenge(_FakeWebSocket([RuntimeError("closed")]), policy=WsPolicy()) is None

    monkeypatch.setattr(
        gateway,
        "_build_device_auth",
        lambda nonce, **_kwargs: {
            "role": "operator",
            "scopes": ["operator.read"],
            "device_token": "device-token",
            "device": {"id": f"device-{nonce}"},
        },
    )
    ok_ws = _FakeWebSocket(
        [
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "abc"}}),
            json.dumps({"type": "event", "event": "ignored"}),
            json.dumps({"type": "res", "ok": True, "payload": {"type": "hello-ok"}}),
        ]
    )
    ok, error = await gateway._handshake(ok_ws, "gateway-token", timeout=1, policy=WsPolicy())
    assert (ok, error) == (True, None)
    assert json.loads(ok_ws.sent[0])["params"]["device"]["id"] == "device-abc"

    failed_ws = _FakeWebSocket(
        [
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "abc"}}),
            json.dumps({"type": "res", "ok": False, "payload": {"type": "hello-ok"}}),
        ]
    )
    assert await gateway._handshake(failed_ws, "gateway-token", timeout=1, policy=WsPolicy()) == (
        False,
        "auth_failed",
    )

    protocol_ws = _FakeWebSocket(
        [
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "abc"}}),
            json.dumps({"type": "res", "ok": True, "payload": {"type": "unexpected"}}),
        ]
    )
    assert await gateway._handshake(protocol_ws, "gateway-token", timeout=1, policy=WsPolicy()) == (
        False,
        "auth_protocol_error",
    )

    timeout_ws = _FakeWebSocket(
        [
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "abc"}}),
            TimeoutError(),
        ]
    )
    assert await gateway._handshake(timeout_ws, "gateway-token", timeout=1, policy=WsPolicy()) == (
        False,
        "handshake_timeout",
    )


@pytest.mark.asyncio
async def test_poll_session_result_handles_success_failures_and_missing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import deep_analysis_gateway as gateway

    class WsPolicy:
        nonce_timeout = 1
        max_history_frames = 2

    async def ok_handshake(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(gateway, "_handshake", ok_handshake)

    success_ws = _HistoryWebSocket(
        {
            "messages": [
                {"message": {"role": "user", "content": "question"}},
                {"message": {"role": "assistant", "content": [{"type": "text", "text": "final text"}]}},
            ]
        }
    )
    monkeypatch.setattr(gateway.websockets, "connect", lambda *_args, **_kwargs: _ConnectContext(success_ws))
    success = await gateway.poll_session_result(
        "http://gateway.test", "token", "session-1", timeout=3, policy=WsPolicy()
    )
    assert success["status"] == "completed"
    assert success["text"] == "final text"
    assert success["msg_count"] == 2

    failed_ws = _HistoryWebSocket(error_message="bad session")
    monkeypatch.setattr(gateway.websockets, "connect", lambda *_args, **_kwargs: _ConnectContext(failed_ws))
    failed = await gateway.poll_session_result(
        "http://gateway.test", "token", "session-1", timeout=3, policy=WsPolicy()
    )
    assert failed["status"] == "error"
    assert failed["error"] == "chat.history failed: bad session"

    no_response_ws = _FakeWebSocket([json.dumps({"type": "event"}), json.dumps({"type": "event"})])
    monkeypatch.setattr(gateway.websockets, "connect", lambda *_args, **_kwargs: _ConnectContext(no_response_ws))
    missing = await gateway.poll_session_result(
        "http://gateway.test",
        "token",
        "session-1",
        timeout=3,
        policy=WsPolicy(),
    )
    assert missing == {"status": "error", "error": "No response received for chat.history request"}

    async def failed_handshake(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        return False, "auth_failed"

    monkeypatch.setattr(gateway, "_handshake", failed_handshake)
    auth_ws = _FakeWebSocket()
    monkeypatch.setattr(gateway.websockets, "connect", lambda *_args, **_kwargs: _ConnectContext(auth_ws))
    auth_error = await gateway.poll_session_result(
        "http://gateway.test",
        "token",
        "session-1",
        timeout=3,
        policy=WsPolicy(),
    )
    assert auth_error == {"status": "error", "error": "auth_failed"}


@pytest.mark.asyncio
async def test_poll_result_stability_degrades_and_terminal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import deep_analysis_poll as gateway

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(gateway, "DEEP_ANALYSIS_TOTAL", StubMetric(metric_calls, "DEEP_ANALYSIS_TOTAL"))

    storage: dict[int, dict[str, object] | None] = {7: None}
    notifications: list[tuple[int, str]] = []
    cleared: list[int] = []

    async def get_stability(record_id: int) -> dict[str, object] | None:
        return storage.get(record_id)

    async def set_stability(record_id: int, data: dict[str, object], **_kwargs: object) -> None:
        storage[record_id] = data

    async def clear_stability(record_id: int) -> None:
        storage[record_id] = None
        cleared.append(record_id)

    async def notify(rec: dict[str, object], reason: str, **_kwargs: object) -> None:
        notifications.append((int(rec["id"]), reason))

    monkeypatch.setattr(gateway, "_get_poll_stability", get_stability)
    monkeypatch.setattr(gateway, "_set_poll_stability", set_stability)
    monkeypatch.setattr(gateway, "_clear_poll_stability", clear_stability)
    monkeypatch.setattr(gateway, "send_deep_analysis_failure_notification", notify)

    policy = _poll_policy(stability_required_hits=3, max_consecutive_errors=2)
    rec = _record()

    first = await gateway._handle_completed_poll_result(
        rec,
        {"status": "completed", "text": "partial", "msg_count": 2},
        utcnow() - timedelta(seconds=3),
        policy=policy,
    )
    assert first == {"id": 7, "action": "skip"}
    assert storage[7]["hit_count"] == 1

    second = await gateway._handle_completed_poll_result(
        rec,
        {"status": "completed", "text": "partial", "msg_count": 2},
        utcnow() - timedelta(seconds=3),
        policy=policy,
    )
    assert second == {"id": 7, "action": "skip"}
    assert storage[7]["hit_count"] == 2

    completed = await gateway._handle_completed_poll_result(
        rec,
        {"status": "completed", "text": "partial", "msg_count": 2},
        utcnow() - timedelta(seconds=3),
        policy=policy,
    )
    assert completed["action"] == "update"
    assert completed["status"] == DeepAnalysisStatus.COMPLETED
    assert completed["_need_success_notify"] is True
    assert storage[7] is None
    assert cleared

    storage[7] = {"first_result": {"text": "first usable result"}, "error_count": 1}
    degraded = await gateway._handle_error_poll_result(
        rec,
        {"status": "error", "error": "upstream unavailable"},
        policy=policy,
    )
    assert degraded["action"] == "update"
    assert degraded["status"] == DeepAnalysisStatus.COMPLETED
    assert degraded["analysis_result"]["root_cause"] == "first usable result"

    retryable = await gateway._handle_error_poll_result(
        rec,
        {"status": "error", "error": "All connection attempts failed", "retryable": True},
        policy=policy,
    )
    assert retryable == {"id": 7, "action": "skip"}

    terminal = await gateway._handle_error_poll_result(
        rec,
        {"status": "error", "error": "permission denied"},
        policy=policy,
    )
    assert terminal["action"] == "update"
    assert terminal["status"] == DeepAnalysisStatus.FAILED
    assert notifications == [(7, "permission denied")]


@pytest.mark.asyncio
async def test_poll_timeout_missing_session_exception_and_dispatch_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import deep_analysis_poll as gateway

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(gateway, "DEEP_ANALYSIS_TOTAL", StubMetric(metric_calls, "DEEP_ANALYSIS_TOTAL"))

    notified: list[str] = []

    async def notify(_rec: dict[str, object], reason: str, **_kwargs: object) -> None:
        notified.append(reason)

    async def clear_stability(_record_id: int) -> None:
        return None

    monkeypatch.setattr(gateway, "send_deep_analysis_failure_notification", notify)
    monkeypatch.setattr(gateway, "_clear_poll_stability", clear_stability)

    policy = _poll_policy(timeout_seconds=10, poll_initial_delay_seconds=5)
    timed_out = await gateway._handle_poll_timeout(
        _record(created_at=utcnow() - timedelta(seconds=20)),
        utcnow() - timedelta(seconds=20),
        policy=policy,
    )
    assert timed_out is not None
    assert timed_out["status"] == DeepAnalysisStatus.FAILED
    assert notified[-1] == "Timeout failure"

    early_missing = await gateway._handle_missing_session_key(
        _record(gateway_session_key="", created_at=utcnow()),
        utcnow(),
        policy=policy,
    )
    assert early_missing == {"id": 7, "action": "skip"}

    late_missing = await gateway._handle_missing_session_key(
        _record(gateway_session_key="", created_at=utcnow() - timedelta(seconds=20)),
        utcnow() - timedelta(seconds=20),
        policy=policy,
    )
    assert late_missing is not None
    assert late_missing["status"] == DeepAnalysisStatus.FAILED
    assert notified[-1] == "No session_key - gateway trigger failed"

    pending = await gateway._handle_poll_result(
        _record(),
        {"status": "pending"},
        utcnow() - timedelta(seconds=1),
        policy=policy,
    )
    assert pending == {"id": 7, "action": "skip"}

    async def boom(_rec: dict[str, object], **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("poll exploded")

    monkeypatch.setattr(gateway, "_fetch_poll_result", boom)
    crashed = await gateway._poll_single_record(_record(), policy=policy)
    assert crashed["action"] == "update"
    assert crashed["status"] == DeepAnalysisStatus.FAILED
    assert crashed["analysis_result"]["error"] == "poll exploded"


@pytest.mark.asyncio
async def test_forward_to_openclaw_disabled_fallback_circuit_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import deep_analysis_trigger as gateway

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(gateway, "FORWARD_DELIVERY_TOTAL", StubMetric(metric_calls, "FORWARD_DELIVERY_TOTAL"))
    monkeypatch.setattr(
        gateway,
        "FORWARD_DELIVERY_DURATION_SECONDS",
        StubMetric(metric_calls, "FORWARD_DELIVERY_DURATION_SECONDS"),
    )

    class PassBreaker:
        async def call_async(self, fn: Any, *args: object, **kwargs: object) -> object:
            return await fn(*args, **kwargs)

    class OpenBreaker:
        async def call_async(self, _fn: Any, *_args: object, **_kwargs: object) -> object:
            raise CircuitBreakerOpenException("gateway")

    class ErrorBreaker:
        async def call_async(self, _fn: Any, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("network down")

    dependencies = DeepAnalysisForwardDependencies(http_client=object(), circuit_breaker=PassBreaker())
    webhook_data = webhook_data_from_mapping(
        {"source": "prometheus", "headers": {"x": "y"}, "parsed_data": {"summary": "alert"}}
    )

    disabled = await gateway.forward_to_deep_analysis(
        webhook_data,
        {"summary": "analysis"},
        policy=_trigger_policy(enabled=False),
        dependencies=dependencies,
    )
    assert disabled == {"status": "disabled"}

    async def degraded_gateway(*_args: object, **_kwargs: object) -> dict[str, object]:
        return degraded_forward_result("gateway unavailable")

    async def local_ai(data: dict[str, object]) -> dict[str, object]:
        assert data["source"] == "prometheus"
        assert "headers" in data
        return {"status": "local-ai", "summary": "fallback"}

    monkeypatch.setattr(gateway, "request_gateway_analysis", degraded_gateway)
    monkeypatch.setattr(gateway, "analyze_webhook_with_ai", local_ai)
    fallback = await gateway.forward_to_deep_analysis(
        webhook_data,
        {"summary": "analysis"},
        policy=_trigger_policy(),
        dependencies=dependencies,
    )
    assert fallback == {"status": "local-ai", "summary": "fallback"}

    circuit_broken = await gateway.forward_to_deep_analysis(
        webhook_data,
        {"summary": "analysis"},
        policy=_trigger_policy(),
        dependencies=DeepAnalysisForwardDependencies(http_client=object(), circuit_breaker=OpenBreaker()),
    )
    assert circuit_broken == {"status": "circuit_broken"}

    errored = await gateway.forward_to_deep_analysis(
        webhook_data,
        {"summary": "analysis"},
        policy=_trigger_policy(),
        dependencies=DeepAnalysisForwardDependencies(http_client=object(), circuit_breaker=ErrorBreaker()),
    )
    assert errored == {"status": "error", "message": "network down"}

    statuses = [
        args[1]
        for name, args, _kwargs, action, _value in metric_calls
        if name == "FORWARD_DELIVERY_TOTAL" and action == "inc"
    ]
    assert {"disabled", "local-ai", "circuit_broken", "error"} <= set(statuses)


@pytest.mark.asyncio
async def test_analyze_with_openclaw_disabled_nondict_trace_and_empty_token_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import deep_analysis_trigger as gateway

    async def fake_prompt() -> str:
        return "prompt-template"

    class PassBreaker:
        async def call_async(self, fn: Any, *args: object, **kwargs: object) -> object:
            return await fn(*args, **kwargs)

    monkeypatch.setattr(gateway, "load_deep_analysis_prompt_template", fake_prompt)
    monkeypatch.setattr(gateway, "get_prompt_source", lambda _kind: "test-source")
    monkeypatch.setattr(gateway, "get_current_trace_id", lambda: "trace-123")

    disabled = await gateway.request_gateway_analysis(
        webhook_data_from_mapping({"source": "prometheus", "parsed_data": {"RuleName": "A"}}),
        policy=_trigger_policy(enabled=False),
        dependencies=DeepAnalysisForwardDependencies(http_client=object(), circuit_breaker=PassBreaker()),
    )
    assert disabled["status"] == "degraded"

    client = _PostClient([_PostResponse({"runId": "run-ok"})])
    result = await gateway.request_gateway_analysis(
        {"source": "raw", "parsed_data": "plain text"},
        user_question="why?",
        policy=_trigger_policy(hooks_token=""),
        dependencies=DeepAnalysisForwardDependencies(http_client=client, circuit_breaker=PassBreaker()),
    )
    assert result["_gateway_run_id"] == "run-ok"
    posted_url, posted_kwargs = client.calls[0]
    assert posted_url == "http://gateway.test/hooks/agent"
    assert posted_kwargs["headers"]["Authorization"] == "Bearer "
    assert posted_kwargs["headers"]["X-Trace-Id"] == "trace-123"
    body = json.loads(posted_kwargs["content"])
    assert body["timeoutSeconds"] == 60
    assert "why?" in body["message"]
    assert '"raw":"plain text"' in body["message"]


@pytest.mark.asyncio
async def test_analyze_with_openclaw_retry_degrade_raise_and_parse_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import deep_analysis_trigger as gateway

    async def fake_prompt() -> str:
        return "prompt-template"

    class PassBreaker:
        async def call_async(self, fn: Any, *args: object, **kwargs: object) -> object:
            return await fn(*args, **kwargs)

    class OpenBreaker:
        async def call_async(self, _fn: Any, *_args: object, **_kwargs: object) -> object:
            raise CircuitBreakerOpenException("gateway")

    monkeypatch.setattr(gateway, "load_deep_analysis_prompt_template", fake_prompt)
    monkeypatch.setattr(gateway, "get_prompt_source", lambda _kind: "test-source")

    async def sanitize(data: dict[str, object], **_kwargs: object) -> dict[str, object]:
        return data

    monkeypatch.setattr(gateway, "sanitize_for_ai_async", sanitize)

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    retry_client = _PostClient([RuntimeError("first failed"), _PostResponse({"runId": "run-after-retry"})])
    retry_result = await gateway.request_gateway_analysis(
        webhook_data_from_mapping({"source": "prometheus", "parsed_data": {"RuleName": "Retry"}}),
        policy=_trigger_policy(max_retries=2, retry_sleep_seconds=0.25),
        dependencies=DeepAnalysisForwardDependencies(http_client=retry_client, circuit_breaker=PassBreaker()),
        sleep=fake_sleep,
    )
    assert retry_result["_gateway_run_id"] == "run-after-retry"
    assert sleeps == [0.25]

    degraded = await gateway.request_gateway_analysis(
        webhook_data_from_mapping({"source": "prometheus", "parsed_data": {"RuleName": "Circuit"}}),
        policy=_trigger_policy(enable_degradation=True),
        dependencies=DeepAnalysisForwardDependencies(http_client=_PostClient([]), circuit_breaker=OpenBreaker()),
    )
    assert degraded["status"] == "degraded"
    assert "CircuitBreaker" in degraded["_degraded_reason"]

    with pytest.raises(CircuitBreakerOpenException):
        await gateway.request_gateway_analysis(
            webhook_data_from_mapping({"source": "prometheus", "parsed_data": {"RuleName": "Circuit"}}),
            policy=_trigger_policy(enable_degradation=False),
            dependencies=DeepAnalysisForwardDependencies(http_client=_PostClient([]), circuit_breaker=OpenBreaker()),
        )

    all_fail = await gateway.request_gateway_analysis(
        webhook_data_from_mapping({"source": "prometheus", "parsed_data": {"RuleName": "Fail"}}),
        policy=_trigger_policy(max_retries=1, enable_degradation=True),
        dependencies=DeepAnalysisForwardDependencies(
            http_client=_PostClient([RuntimeError("permanent")]),
            circuit_breaker=PassBreaker(),
        ),
    )
    assert all_fail["status"] == "degraded"
    assert "permanent" in all_fail["_degraded_reason"]

    with pytest.raises(Exception, match="Openclaw request failed: permanent"):
        await gateway.request_gateway_analysis(
            webhook_data_from_mapping({"source": "prometheus", "parsed_data": {"RuleName": "Fail"}}),
            policy=_trigger_policy(max_retries=1, enable_degradation=False),
            dependencies=DeepAnalysisForwardDependencies(
                http_client=_PostClient([RuntimeError("permanent")]),
                circuit_breaker=PassBreaker(),
            ),
        )

    parse_degraded = await gateway.request_gateway_analysis(
        webhook_data_from_mapping({"source": "prometheus", "parsed_data": {"RuleName": "Parse"}}),
        policy=_trigger_policy(enable_degradation=True),
        dependencies=DeepAnalysisForwardDependencies(
            http_client=_PostClient([_PostResponse(["not", "dict"])]),
            circuit_breaker=PassBreaker(),
        ),
    )
    assert parse_degraded["status"] == "degraded"
    assert "JSON object" in parse_degraded["_degraded_reason"]

    with pytest.raises(ValueError, match="JSON object"):
        await gateway.request_gateway_analysis(
            webhook_data_from_mapping({"source": "prometheus", "parsed_data": {"RuleName": "Parse"}}),
            policy=_trigger_policy(enable_degradation=False),
            dependencies=DeepAnalysisForwardDependencies(
                http_client=_PostClient([_PostResponse(["not", "dict"])]),
                circuit_breaker=PassBreaker(),
            ),
        )
