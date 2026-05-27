from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from core import json
from core.circuit_breaker import CircuitBreakerOpenException
from core.datetime_utils import utcnow
from services.forwarding.circuit_breakers import OpenClawForwardDependencies
from services.forwarding.policies import OpenClawTriggerPolicy
from services.webhooks.types import DeepAnalysisStatus, degraded_forward_result, webhook_data_from_mapping


class _BoundMetric:
    def __init__(self, sink: list[tuple[str, tuple[object, ...], dict[str, object], str, object]], name: str, args: tuple[object, ...], kwargs: dict[str, object]) -> None:
        self._sink = sink
        self._name = name
        self._args = args
        self._kwargs = kwargs

    def inc(self, amount: object = 1) -> None:
        self._sink.append((self._name, self._args, self._kwargs, "inc", amount))

    def observe(self, value: object) -> None:
        self._sink.append((self._name, self._args, self._kwargs, "observe", value))


class _Metric:
    def __init__(self, sink: list[tuple[str, tuple[object, ...], dict[str, object], str, object]], name: str) -> None:
        self._sink = sink
        self._name = name

    def labels(self, *args: object, **kwargs: object) -> _BoundMetric:
        return _BoundMetric(self._sink, self._name, args, kwargs)


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
        raise asyncio.TimeoutError


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
            return json.dumps(
                {"type": "res", "id": request_id, "ok": False, "error": {"message": self.error_message}}
            )
        return json.dumps({"type": "res", "id": request_id, "ok": True, "payload": self.response_payload})


class _ConnectContext:
    def __init__(self, ws: _FakeWebSocket) -> None:
        self.ws = ws

    async def __aenter__(self) -> _FakeWebSocket:
        return self.ws

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _poll_policy(**overrides: object) -> Any:
    from services.analysis.openclaw import OpenClawPollPolicy

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
    return OpenClawPollPolicy(**values)


def _trigger_policy(**overrides: object) -> OpenClawTriggerPolicy:
    values: dict[str, object] = {
        "enabled": True,
        "timeout_seconds": 60,
        "platform": "openclaw",
        "gateway_url": "http://openclaw.test",
        "hooks_token": "hooks-token",
        "connect_timeout": 2.0,
        "enable_degradation": True,
    }
    values.update(overrides)
    return OpenClawTriggerPolicy(**values)


def _record(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": 7,
        "webhook_event_id": 11,
        "engine": "openclaw",
        "openclaw_session_key": "session-1",
        "openclaw_run_id": "run-1",
        "created_at": utcnow(),
        "status": DeepAnalysisStatus.PENDING,
        "analysis_result": None,
        "duration_seconds": 0,
        "poll_attempts": 0,
        "last_polled_at": None,
    }
    values.update(overrides)
    return values


def test_openclaw_json_url_and_history_parsing_helpers() -> None:
    from services.analysis import openclaw

    assert openclaw._loads_dict(b'{"ok": true}') == {"ok": True}
    assert openclaw._loads_dict(b"\xff") is None
    assert openclaw._loads_dict("[1, 2]") is None
    assert openclaw._loads_dict({"already": "dict"}) is None
    assert openclaw._http_to_ws_url("https://gateway.test/") == "wss://gateway.test/ws"
    assert openclaw._http_to_ws_url("http://gateway.test") == "ws://gateway.test/ws"
    assert openclaw._http_to_ws_url("gateway.test") == "ws://gateway.test/ws"

    plain_frame = openclaw._build_connect_frame("token")
    assert plain_frame["params"]["client"]["mode"] == "backend"
    assert plain_frame["params"]["auth"] == {"token": "token"}

    device_frame = openclaw._build_connect_frame(
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

    assert openclaw._parse_history_messages([]) == {"status": "pending"}
    assert openclaw._parse_history_messages([{"message": {"role": "user", "content": "wait"}}]) == {
        "status": "pending"
    }
    assert openclaw._parse_history_messages(
        [{"message": {"role": "assistant", "content": [{"type": "tool_use"}]}}]
    ) == {"status": "pending"}
    completed = openclaw._parse_history_messages(
        [{"message": {"role": "assistant", "content": [{"type": "text", "text": "root cause"}]}}]
    )
    assert completed["status"] == "completed"
    assert completed["text"] == "root cause"


@pytest.mark.asyncio
async def test_openclaw_challenge_and_handshake_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.analysis import openclaw

    class WsPolicy:
        nonce_timeout = 1
        max_history_frames = 3

    challenge_ws = _FakeWebSocket(
        [json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "nonce-1"}})]
    )
    assert await openclaw._try_recv_challenge(challenge_ws, policy=WsPolicy()) == "nonce-1"
    assert await openclaw._try_recv_challenge(_FakeWebSocket([asyncio.TimeoutError()]), policy=WsPolicy()) is None
    assert await openclaw._try_recv_challenge(_FakeWebSocket([RuntimeError("closed")]), policy=WsPolicy()) is None

    monkeypatch.setattr(
        openclaw,
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
    ok, error = await openclaw._handshake(ok_ws, "gateway-token", timeout=1, policy=WsPolicy())
    assert (ok, error) == (True, None)
    assert json.loads(ok_ws.sent[0])["params"]["device"]["id"] == "device-abc"

    failed_ws = _FakeWebSocket(
        [
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "abc"}}),
            json.dumps({"type": "res", "ok": False, "payload": {"type": "hello-ok"}}),
        ]
    )
    assert await openclaw._handshake(failed_ws, "gateway-token", timeout=1, policy=WsPolicy()) == (
        False,
        "auth_failed",
    )

    protocol_ws = _FakeWebSocket(
        [
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "abc"}}),
            json.dumps({"type": "res", "ok": True, "payload": {"type": "unexpected"}}),
        ]
    )
    assert await openclaw._handshake(protocol_ws, "gateway-token", timeout=1, policy=WsPolicy()) == (
        False,
        "auth_protocol_error",
    )

    timeout_ws = _FakeWebSocket(
        [
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "abc"}}),
            asyncio.TimeoutError(),
        ]
    )
    assert await openclaw._handshake(timeout_ws, "gateway-token", timeout=1, policy=WsPolicy()) == (
        False,
        "handshake_timeout",
    )


@pytest.mark.asyncio
async def test_poll_session_result_handles_success_failures_and_missing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import openclaw

    class WsPolicy:
        nonce_timeout = 1
        max_history_frames = 2

    async def ok_handshake(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(openclaw, "_handshake", ok_handshake)

    success_ws = _HistoryWebSocket(
        {
            "messages": [
                {"message": {"role": "user", "content": "question"}},
                {"message": {"role": "assistant", "content": [{"type": "text", "text": "final text"}]}},
            ]
        }
    )
    monkeypatch.setattr(openclaw.websockets, "connect", lambda *_args, **_kwargs: _ConnectContext(success_ws))
    success = await openclaw.poll_session_result("http://gateway.test", "token", "session-1", timeout=3, policy=WsPolicy())
    assert success["status"] == "completed"
    assert success["text"] == "final text"
    assert success["msg_count"] == 2

    failed_ws = _HistoryWebSocket(error_message="bad session")
    monkeypatch.setattr(openclaw.websockets, "connect", lambda *_args, **_kwargs: _ConnectContext(failed_ws))
    failed = await openclaw.poll_session_result("http://gateway.test", "token", "session-1", timeout=3, policy=WsPolicy())
    assert failed["status"] == "error"
    assert failed["error"] == "chat.history failed: bad session"

    no_response_ws = _FakeWebSocket([json.dumps({"type": "event"}), json.dumps({"type": "event"})])
    monkeypatch.setattr(openclaw.websockets, "connect", lambda *_args, **_kwargs: _ConnectContext(no_response_ws))
    missing = await openclaw.poll_session_result(
        "http://gateway.test",
        "token",
        "session-1",
        timeout=3,
        policy=WsPolicy(),
    )
    assert missing == {"status": "error", "error": "No response received for chat.history request"}

    async def failed_handshake(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        return False, "auth_failed"

    monkeypatch.setattr(openclaw, "_handshake", failed_handshake)
    auth_ws = _FakeWebSocket()
    monkeypatch.setattr(openclaw.websockets, "connect", lambda *_args, **_kwargs: _ConnectContext(auth_ws))
    auth_error = await openclaw.poll_session_result(
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
    from services.analysis import openclaw

    metric_calls: list[tuple[str, tuple[object, ...], dict[str, object], str, object]] = []
    monkeypatch.setattr(openclaw, "DEEP_ANALYSIS_TOTAL", _Metric(metric_calls, "DEEP_ANALYSIS_TOTAL"))

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

    monkeypatch.setattr(openclaw, "_get_poll_stability", get_stability)
    monkeypatch.setattr(openclaw, "_set_poll_stability", set_stability)
    monkeypatch.setattr(openclaw, "_clear_poll_stability", clear_stability)
    monkeypatch.setattr(openclaw, "send_deep_analysis_failure_notification", notify)

    policy = _poll_policy(stability_required_hits=3, max_consecutive_errors=2)
    rec = _record()

    first = await openclaw._handle_completed_poll_result(
        rec,
        {"status": "completed", "text": "partial", "msg_count": 2},
        utcnow() - timedelta(seconds=3),
        policy=policy,
    )
    assert first == {"id": 7, "action": "skip"}
    assert storage[7]["hit_count"] == 1

    second = await openclaw._handle_completed_poll_result(
        rec,
        {"status": "completed", "text": "partial", "msg_count": 2},
        utcnow() - timedelta(seconds=3),
        policy=policy,
    )
    assert second == {"id": 7, "action": "skip"}
    assert storage[7]["hit_count"] == 2

    completed = await openclaw._handle_completed_poll_result(
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
    degraded = await openclaw._handle_error_poll_result(
        rec,
        {"status": "error", "error": "upstream unavailable"},
        policy=policy,
    )
    assert degraded["action"] == "update"
    assert degraded["status"] == DeepAnalysisStatus.COMPLETED
    assert degraded["analysis_result"]["root_cause"] == "first usable result"

    retryable = await openclaw._handle_error_poll_result(
        rec,
        {"status": "error", "error": "All connection attempts failed", "retryable": True},
        policy=policy,
    )
    assert retryable == {"id": 7, "action": "skip"}

    terminal = await openclaw._handle_error_poll_result(
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
    from services.analysis import openclaw

    metric_calls: list[tuple[str, tuple[object, ...], dict[str, object], str, object]] = []
    monkeypatch.setattr(openclaw, "DEEP_ANALYSIS_TOTAL", _Metric(metric_calls, "DEEP_ANALYSIS_TOTAL"))

    notified: list[str] = []

    async def notify(_rec: dict[str, object], reason: str, **_kwargs: object) -> None:
        notified.append(reason)

    async def clear_stability(_record_id: int) -> None:
        return None

    monkeypatch.setattr(openclaw, "send_deep_analysis_failure_notification", notify)
    monkeypatch.setattr(openclaw, "_clear_poll_stability", clear_stability)

    policy = _poll_policy(timeout_seconds=10, poll_initial_delay_seconds=5)
    timed_out = await openclaw._handle_poll_timeout(
        _record(created_at=utcnow() - timedelta(seconds=20)),
        utcnow() - timedelta(seconds=20),
        policy=policy,
    )
    assert timed_out is not None
    assert timed_out["status"] == DeepAnalysisStatus.FAILED
    assert notified[-1] == "超时失败"

    early_missing = await openclaw._handle_missing_session_key(
        _record(openclaw_session_key="", created_at=utcnow()),
        utcnow(),
        policy=policy,
    )
    assert early_missing == {"id": 7, "action": "skip"}

    late_missing = await openclaw._handle_missing_session_key(
        _record(openclaw_session_key="", created_at=utcnow() - timedelta(seconds=20)),
        utcnow() - timedelta(seconds=20),
        policy=policy,
    )
    assert late_missing is not None
    assert late_missing["status"] == DeepAnalysisStatus.FAILED
    assert notified[-1] == "无 session_key - OpenClaw 触发失败"

    pending = await openclaw._handle_poll_result(
        _record(),
        {"status": "pending"},
        utcnow() - timedelta(seconds=1),
        policy=policy,
    )
    assert pending == {"id": 7, "action": "skip"}

    async def boom(_rec: dict[str, object], **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("poll exploded")

    monkeypatch.setattr(openclaw, "_fetch_poll_result", boom)
    crashed = await openclaw._poll_single_record(_record(), policy=policy)
    assert crashed["action"] == "update"
    assert crashed["status"] == DeepAnalysisStatus.FAILED
    assert crashed["analysis_result"]["error"] == "poll exploded"


@pytest.mark.asyncio
async def test_forward_to_openclaw_disabled_fallback_circuit_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import openclaw

    metric_calls: list[tuple[str, tuple[object, ...], dict[str, object], str, object]] = []
    monkeypatch.setattr(openclaw, "FORWARD_DELIVERY_TOTAL", _Metric(metric_calls, "FORWARD_DELIVERY_TOTAL"))
    monkeypatch.setattr(
        openclaw,
        "FORWARD_DELIVERY_DURATION_SECONDS",
        _Metric(metric_calls, "FORWARD_DELIVERY_DURATION_SECONDS"),
    )

    class PassBreaker:
        async def call_async(self, fn: Any, *args: object, **kwargs: object) -> object:
            return await fn(*args, **kwargs)

    class OpenBreaker:
        async def call_async(self, _fn: Any, *_args: object, **_kwargs: object) -> object:
            raise CircuitBreakerOpenException("openclaw")

    class ErrorBreaker:
        async def call_async(self, _fn: Any, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("network down")

    dependencies = OpenClawForwardDependencies(http_client=object(), circuit_breaker=PassBreaker())
    webhook_data = webhook_data_from_mapping(
        {"source": "prometheus", "headers": {"x": "y"}, "parsed_data": {"summary": "alert"}}
    )

    disabled = await openclaw.forward_to_openclaw(
        webhook_data,
        {"summary": "analysis"},
        policy=_trigger_policy(enabled=False),
        dependencies=dependencies,
    )
    assert disabled == {"status": "disabled"}

    async def degraded_openclaw(*_args: object, **_kwargs: object) -> dict[str, object]:
        return degraded_forward_result("gateway unavailable")

    async def local_ai(data: dict[str, object]) -> dict[str, object]:
        assert data["source"] == "prometheus"
        assert "headers" in data
        return {"status": "local-ai", "summary": "fallback"}

    monkeypatch.setattr(openclaw, "analyze_with_openclaw", degraded_openclaw)
    monkeypatch.setattr(openclaw, "analyze_webhook_with_ai", local_ai)
    fallback = await openclaw.forward_to_openclaw(
        webhook_data,
        {"summary": "analysis"},
        policy=_trigger_policy(),
        dependencies=dependencies,
    )
    assert fallback == {"status": "local-ai", "summary": "fallback"}

    circuit_broken = await openclaw.forward_to_openclaw(
        webhook_data,
        {"summary": "analysis"},
        policy=_trigger_policy(),
        dependencies=OpenClawForwardDependencies(http_client=object(), circuit_breaker=OpenBreaker()),
    )
    assert circuit_broken == {"status": "circuit_broken"}

    errored = await openclaw.forward_to_openclaw(
        webhook_data,
        {"summary": "analysis"},
        policy=_trigger_policy(),
        dependencies=OpenClawForwardDependencies(http_client=object(), circuit_breaker=ErrorBreaker()),
    )
    assert errored == {"status": "error", "message": "network down"}

    statuses = [args[1] for name, args, _kwargs, action, _value in metric_calls if name == "FORWARD_DELIVERY_TOTAL" and action == "inc"]
    assert {"disabled", "local-ai", "circuit_broken", "error"} <= set(statuses)
