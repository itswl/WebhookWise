"""OpenClaw 深度分析集成 — 触发、轮询、策略配置、WebSocket 客户端。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac as hmac_mod
import platform
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import httpx
import websockets

from core import json
from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreakerOpenException
from core.datetime_utils import utcnow
from core.http_client import get_http_client
from core.logger import get_logger, mask_url
from core.observability.metrics import (
    DEEP_ANALYSIS_TOTAL,
    FORWARD_DELIVERY_DURATION_SECONDS,
    FORWARD_DELIVERY_TOTAL,
)
from core.observability.tracing import get_current_trace_id
from services.analysis.ai_prompt import DEEP_ANALYSIS_PROMPT_KIND, get_prompt_source, load_deep_analysis_prompt_template
from services.forwarding.circuit_breakers import OpenClawForwardDependencies, build_openclaw_forward_dependencies
from services.forwarding.policies import OpenClawTriggerPolicy
from services.operations.deep_analysis_notifications import (
    send_deep_analysis_failure_notification,
    send_deep_analysis_success_notification,
)
from services.webhooks.types import AnalysisResult, DeepAnalysisStatus, ForwardResult, WebhookData

logger = get_logger("openclaw")

_JSON_UTF8_CONTENT_TYPE = "application/json; charset=utf-8"
MANUAL_RETRY_STARTED_AT_KEY = "_manual_retry_started_at"

# ═══════════════════════════════════════════════════════════════════════════════
# Poll 策略配置
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class OpenClawPollPolicy:
    timeout_seconds: int
    poll_timeout_seconds: int
    poll_initial_delay_seconds: int
    poll_max_delay_seconds: int
    poll_backoff_multiplier: float
    http_api_url: str
    gateway_url: str
    gateway_token: str
    hooks_token: str
    connect_timeout_seconds: float
    stability_required_hits: int
    max_consecutive_errors: int
    enable_degradation: bool
    notification_webhook_url: str

    @classmethod
    def from_config(cls) -> OpenClawPollPolicy:
        cfg = get_config_manager()
        return cls(
            timeout_seconds=int(cfg.openclaw.OPENCLAW_TIMEOUT_SECONDS),
            poll_timeout_seconds=max(1, int(cfg.openclaw.OPENCLAW_POLL_TIMEOUT)),
            poll_initial_delay_seconds=max(1, int(cfg.openclaw.OPENCLAW_POLL_INITIAL_DELAY_SECONDS)),
            poll_max_delay_seconds=max(
                max(1, int(cfg.openclaw.OPENCLAW_POLL_INITIAL_DELAY_SECONDS)),
                int(cfg.openclaw.OPENCLAW_POLL_MAX_DELAY_SECONDS),
            ),
            poll_backoff_multiplier=max(1.0, float(cfg.openclaw.OPENCLAW_POLL_BACKOFF_MULTIPLIER)),
            http_api_url=str(cfg.openclaw.OPENCLAW_HTTP_API_URL).strip(),
            gateway_url=str(cfg.openclaw.OPENCLAW_GATEWAY_URL).strip(),
            gateway_token=str(cfg.openclaw.OPENCLAW_GATEWAY_TOKEN),
            hooks_token=str(cfg.openclaw.OPENCLAW_HOOKS_TOKEN or cfg.openclaw.OPENCLAW_GATEWAY_TOKEN),
            connect_timeout_seconds=max(1.0, float(cfg.openclaw.OPENCLAW_CONNECT_TIMEOUT)),
            stability_required_hits=max(1, int(cfg.openclaw.OPENCLAW_STABILITY_REQUIRED_HITS)),
            max_consecutive_errors=int(cfg.openclaw.OPENCLAW_MAX_CONSECUTIVE_ERRORS),
            enable_degradation=bool(cfg.openclaw.OPENCLAW_ENABLE_DEGRADATION),
            notification_webhook_url=str(cfg.notifications.DEEP_ANALYSIS_FEISHU_WEBHOOK),
        )

    @property
    def has_http_api(self) -> bool:
        return bool(self.http_api_url.strip())

    @property
    def http_poll_timeout(self) -> float:
        return float(self.poll_timeout_seconds)

    @property
    def http_connect_timeout(self) -> float:
        return max(1.0, min(float(self.connect_timeout_seconds), self.http_poll_timeout))

    @property
    def poll_claim_lease_seconds(self) -> int:
        return max(30, self.poll_timeout_seconds * 3, 90) + 30

    def clamp_delay_to_timeout(self, delay_seconds: int, created_at: datetime | None) -> int:
        if created_at is None:
            return delay_seconds
        elapsed = (utcnow() - created_at).total_seconds()
        remaining = int(self.timeout_seconds - elapsed)
        if remaining <= 0:
            return 1
        return max(1, min(delay_seconds, remaining))

    def delay_for_attempt(self, poll_attempts: int) -> int:
        normalized_attempts = max(0, int(poll_attempts))
        delay = float(self.poll_initial_delay_seconds)
        for _ in range(normalized_attempts):
            delay *= self.poll_backoff_multiplier
            if delay >= self.poll_max_delay_seconds:
                return self.poll_max_delay_seconds
        return max(1, int(delay))

    def http_auth_headers(self, trace_id: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.hooks_token}"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        return headers


@dataclass(frozen=True, slots=True)
class OpenClawWsPolicy:
    device_id: str
    device_private_key_b64: str
    device_token: str
    gateway_token: str
    nonce_timeout: float

    @classmethod
    def from_config(cls) -> OpenClawWsPolicy:
        cfg = get_config_manager()
        return cls(
            device_id=str(cfg.openclaw.OPENCLAW_DEVICE_ID),
            device_private_key_b64=str(cfg.openclaw.OPENCLAW_DEVICE_PRIVATE_KEY_PEM),
            device_token=str(cfg.openclaw.OPENCLAW_DEVICE_TOKEN),
            gateway_token=str(cfg.openclaw.OPENCLAW_GATEWAY_TOKEN),
            nonce_timeout=float(cfg.openclaw.OPENCLAW_NONCE_TIMEOUT),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP 轮询
# ═══════════════════════════════════════════════════════════════════════════════


def _describe_exception(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return repr(exc)


async def poll_openclaw_final(
    session_key: str,
    *,
    policy: OpenClawPollPolicy,
    http_client: Any,
    trace_id: str | None = None,
    retry_count: int = 3,
) -> WebhookData:
    base_url = policy.http_api_url.rstrip("/")
    headers = {**policy.http_auth_headers(trace_id), "Connection": "close"}
    timeout = httpx.Timeout(
        connect=policy.http_connect_timeout,
        read=policy.http_poll_timeout,
        write=policy.http_connect_timeout,
        pool=policy.http_connect_timeout,
    )
    last_error = None
    transport_error = False

    for attempt in range(retry_count):
        started = time.monotonic()
        try:
            url = f"{base_url}/sessions/{session_key}/final"
            logger.debug("HTTP /final 请求 (尝试 %s/%s): %s", attempt + 1, retry_count, url)
            response = await http_client.get(url, headers=headers, timeout=timeout)
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if response.status_code == 404:
                last_error = "Session not found"
                logger.warning("Session 未找到 (尝试 %d/%d elapsed=%sms)", attempt + 1, retry_count, elapsed_ms)
                continue
            if response.status_code in (202, 204):
                last_error = "分析进行中"
                logger.debug("分析进行中 (尝试 %s/%s elapsed=%sms)", attempt + 1, retry_count, elapsed_ms)
                continue
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    "HTTP /final 返回非 200 status=%s attempt=%s/%s elapsed=%sms",
                    response.status_code,
                    attempt + 1,
                    retry_count,
                    elapsed_ms,
                )
                continue

            try:
                raw = response.json()
            except ValueError:
                last_error = "Invalid JSON response"
                logger.warning(
                    "HTTP /final 返回无效 JSON (尝试 %s/%s elapsed=%sms)", attempt + 1, retry_count, elapsed_ms
                )
                continue
            if not isinstance(raw, dict):
                last_error = "Invalid JSON response"
                continue

            data: WebhookData = raw
            is_final = data.get("isFinal")
            is_processing = data.get("isProcessing", False)
            text = data.get("text", "")
            msg_count = int(data.get("messageCount", 0) or 0)

            if is_processing is True:
                last_error = "分析进行中"
                continue
            if text and is_final is not False:
                result: WebhookData = {"status": "completed", "text": text, "msg_count": msg_count}
                if is_final is True:
                    result["is_final"] = True
                return result
            if is_final is False or not is_final:
                last_error = "分析进行中"
                continue
            last_error = "No text content"
        except httpx.ReadTimeout as e:
            last_error = f"ReadTimeout after {policy.http_poll_timeout:g}s"
            logger.info(
                "HTTP /final 等待超时，按 pending 处理 attempt=%s/%s timeout=%ss error_type=%s error=%s",
                attempt + 1,
                retry_count,
                policy.http_poll_timeout,
                type(e).__name__,
                _describe_exception(e),
            )
            return {"status": "pending", "error": last_error}
        except httpx.TimeoutException as e:
            transport_error = True
            last_error = _describe_exception(e)
            logger.warning(
                "HTTP 轮询超时 attempt=%s/%s error_type=%s error=%s",
                attempt + 1,
                retry_count,
                type(e).__name__,
                last_error,
            )
        except Exception as e:
            transport_error = True
            last_error = _describe_exception(e)
            logger.warning(
                "HTTP 轮询异常 attempt=%s/%s error_type=%s error=%s",
                attempt + 1,
                retry_count,
                type(e).__name__,
                last_error,
            )

    if last_error == "分析进行中":
        return {"status": "pending"}
    if transport_error:
        return {"status": "error", "error": last_error or "HTTP transport error", "retryable": True}
    return {"status": "error", "error": last_error}


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket 客户端
# ═══════════════════════════════════════════════════════════════════════════════


def _loads_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _http_to_ws_url(http_url: str) -> str:
    url = http_url.rstrip("/")
    if url.startswith("https://"):
        return url.replace("https://", "wss://") + "/ws"
    if url.startswith("http://"):
        return url.replace("http://", "ws://") + "/ws"
    return f"ws://{url}/ws"


def _build_connect_frame(token: str, device_auth: dict[str, Any] | None = None) -> dict[str, Any]:
    client_platform = "linux" if device_auth else platform.system().lower()
    client_mode = "cli" if device_auth else "backend"
    frame: dict[str, Any] = {
        "type": "req",
        "id": str(uuid.uuid4()),
        "method": "connect",
        "params": {
            "minProtocol": 3,
            "maxProtocol": 3,
            "client": {"id": "gateway-client", "version": "1.0.0", "platform": client_platform, "mode": client_mode},
            "auth": {"token": token},
        },
    }
    if device_auth:
        params = frame["params"]
        params["role"] = device_auth["role"]
        params["scopes"] = device_auth["scopes"]
        params["auth"]["deviceToken"] = device_auth["device_token"]
        params["device"] = device_auth["device"]
        logger.debug("Device auth attached: deviceId=%s...", device_auth["device"]["id"][:16])
    return frame


def _build_device_auth(
    nonce: str, *, gateway_token: str = "", policy: OpenClawWsPolicy | None = None
) -> dict[str, Any] | None:
    policy = policy or OpenClawWsPolicy.from_config()
    device_id = policy.device_id
    private_key_b64 = policy.device_private_key_b64
    device_token = policy.device_token
    if not device_id or not private_key_b64:
        return None

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    except ImportError:
        logger.warning("cryptography package not installed, skipping device auth")
        return None

    try:
        pem = f"-----BEGIN PRIVATE KEY-----\n{private_key_b64}\n-----END PRIVATE KEY-----\n"
        private_key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            logger.warning("Unsupported private key type for device auth: %s", type(private_key).__name__)
            return None
        pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        pub_b64url = base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")
        signed_at = int(time.time() * 1000)
        signature_gateway_token = gateway_token or policy.gateway_token
        scopes_str = "operator.read"
        payload = (
            f"v2|{device_id}|gateway-client|cli|operator|{scopes_str}|{signed_at}|{signature_gateway_token}|{nonce}"
        )
        signature = private_key.sign(payload.encode())
        sig_b64url = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        return {
            "role": "operator",
            "scopes": ["operator.read"],
            "device_token": device_token,
            "device": {
                "id": device_id,
                "publicKey": pub_b64url,
                "signature": sig_b64url,
                "signedAt": signed_at,
                "nonce": nonce,
            },
        }
    except (ValueError, TypeError) as e:
        logger.warning("Failed to build device auth: %s", e)
        return None


async def _try_recv_challenge(
    ws: Any, timeout: float | None = None, *, policy: OpenClawWsPolicy | None = None
) -> str | None:
    if timeout is None:
        timeout = (policy or OpenClawWsPolicy.from_config()).nonce_timeout
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        frame = _loads_dict(raw)
        if frame and frame.get("type") == "event" and frame.get("event") == "connect.challenge":
            payload = frame.get("payload")
            nonce_val = payload.get("nonce") if isinstance(payload, dict) else None
            if isinstance(nonce_val, str) and nonce_val:
                logger.info("Received connect.challenge, nonce=%s...", nonce_val[:16])
                return nonce_val
    except asyncio.TimeoutError:
        return None
    except (RuntimeError, websockets.WebSocketException) as e:
        logger.debug("Error receiving challenge: %s", e)
        return None
    return None


async def _handshake(
    ws: Any, gateway_token: str, timeout: float, *, policy: OpenClawWsPolicy | None = None
) -> tuple[bool, str | None]:
    try:
        policy = policy or OpenClawWsPolicy.from_config()
        nonce = await _try_recv_challenge(ws, policy=policy)
        device_auth = _build_device_auth(nonce, gateway_token=gateway_token, policy=policy) if nonce else None
        connect_frame = _build_connect_frame(gateway_token, device_auth=device_auth)
        await ws.send(json.dumps(connect_frame))
        response = None
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            frame = _loads_dict(raw)
            if frame and frame.get("type") == "res":
                response = frame
                break
        if not response or response.get("type") != "res":
            return False, "auth_protocol_error"
        if not response.get("ok"):
            return False, "auth_failed"
        payload = response.get("payload", {})
        if payload.get("type") != "hello-ok":
            return False, "auth_protocol_error"
        return True, None
    except asyncio.TimeoutError:
        return False, "handshake_timeout"
    except json.JSONDecodeError:
        return False, "invalid_response"
    except (OSError, RuntimeError, websockets.WebSocketException):
        logger.debug("OpenClaw WebSocket handshake failed", exc_info=True)
        return False, "handshake_error"


def _parse_history_messages(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not messages:
        return {"status": "pending"}
    last_entry = messages[-1]
    msg = last_entry.get("message", last_entry)
    role = msg.get("role", "")
    content = msg.get("content", [])
    if role != "assistant":
        return {"status": "pending"}
    has_tool_use = False
    text_parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type in ("tool_use", "toolUse", "toolCall", "tool_call"):
                has_tool_use = True
            elif item_type == "text":
                t = item.get("text", "")
                if t:
                    text_parts.append(t)
    elif isinstance(content, str) and content:
        text_parts.append(content)
    if has_tool_use:
        return {"status": "pending"}
    final_text = "\n".join(text_parts)
    if not final_text:
        return {"status": "pending"}
    return {"status": "completed", "text": final_text, "message": msg}


async def poll_session_result(
    gateway_url: str,
    gateway_token: str,
    session_key: str,
    timeout: int = 30,
    *,
    policy: OpenClawWsPolicy | None = None,
) -> dict[str, Any]:
    ws_url = _http_to_ws_url(gateway_url)
    start = time.monotonic()
    policy = policy or OpenClawWsPolicy.from_config()
    connect_timeout = min(5, max(1, timeout // 3))
    handshake_timeout = min(15, max(3, timeout // 2))

    try:
        async with websockets.connect(ws_url, open_timeout=connect_timeout, close_timeout=1, max_size=None) as ws:
            ok, err_type = await _handshake(ws, gateway_token, timeout=handshake_timeout, policy=policy)
            if not ok:
                return {"status": "error", "error": err_type or "handshake_failed"}

            request_id = str(uuid.uuid4())
            history_request = {
                "type": "req",
                "id": request_id,
                "method": "chat.history",
                "params": {"sessionKey": session_key},
            }
            await ws.send(json.dumps(history_request))

            max_frames = 50
            for _ in range(max_frames):
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    return {"status": "error", "error": f"Timeout ({timeout}s)"}
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                frame = _loads_dict(raw)
                if not frame or frame.get("type") != "res" or frame.get("id") != request_id:
                    continue
                if not frame.get("ok"):
                    error = frame.get("error", {})
                    return {"status": "error", "error": f"chat.history failed: {error.get('message', 'Unknown error')}"}
                payload = frame.get("payload", {}) or {}
                messages = payload.get("messages", []) or []
                parsed = _parse_history_messages(messages)
                if parsed.get("status") == "completed":
                    parsed["msg_count"] = len(messages)
                return parsed

            return {"status": "error", "error": "No response received for chat.history request"}
    except asyncio.TimeoutError:
        return {"status": "error", "error": f"Timeout ({timeout}s)"}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"Invalid JSON response: {e}"}
    except (OSError, RuntimeError, websockets.WebSocketException) as e:
        return {"status": "error", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# 轮询调度
# ═══════════════════════════════════════════════════════════════════════════════


async def _safe_notify(coro: Any) -> None:
    try:
        await coro
    except Exception as e:
        logger.warning("[Poller] 后台通知失败: %s", e)


def _seconds_until(target: datetime) -> int:
    return max(1, int((target - utcnow()).total_seconds()))


def _clamp_poll_delay_to_timeout(
    delay_seconds: int, created_at: datetime | None, *, policy: OpenClawPollPolicy | None = None
) -> int:
    return (policy or OpenClawPollPolicy.from_config()).clamp_delay_to_timeout(delay_seconds, created_at)


def _poll_claim_lease_seconds(policy: OpenClawPollPolicy | None = None) -> int:
    return (policy or OpenClawPollPolicy.from_config()).poll_claim_lease_seconds


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _poll_timeout_started_at(rec: WebhookData) -> datetime | None:
    analysis_result = rec.get("analysis_result")
    if isinstance(analysis_result, dict):
        manual_retry_started_at = analysis_result.get(MANUAL_RETRY_STARTED_AT_KEY)
        if isinstance(manual_retry_started_at, str) and manual_retry_started_at:
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(manual_retry_started_at)
    created_at = rec.get("created_at")
    return created_at if isinstance(created_at, datetime) else None


def _is_transient_poll_error(error: object) -> bool:
    if not error:
        return False
    text = str(error).lower()
    transient_markers = (
        "all connection attempts failed",
        "connection refused",
        "connection reset",
        "connect call failed",
        "network is unreachable",
        "no route to host",
        "name or service not known",
        "temporary failure",
        "timed out",
        "timeout",
    )
    return any(marker in text for marker in transient_markers)


async def _get_poll_stability(record_id: int) -> WebhookData | None:
    from core.redis_client import redis_get_json_dict
    from core.redis_health import openclaw_poller_stability

    return await redis_get_json_dict(openclaw_poller_stability(record_id))


async def _set_poll_stability(record_id: int, data: WebhookData) -> None:
    from core.redis_client import redis_setex_json
    from core.redis_health import openclaw_poller_stability

    await redis_setex_json(openclaw_poller_stability(record_id), 3600, data)


async def _clear_poll_stability(record_id: int) -> None:
    from core.redis_client import redis_delete
    from core.redis_health import openclaw_poller_stability

    await redis_delete(openclaw_poller_stability(record_id))


async def clear_openclaw_poll_state(record_id: int) -> None:
    await _clear_poll_stability(record_id)


async def poll_openclaw_result_via_http(
    session_key: str,
    retry_count: int = 3,
    *,
    policy: OpenClawPollPolicy | None = None,
    http_client: Any | None = None,
) -> WebhookData:
    policy = policy or OpenClawPollPolicy.from_config()
    return await poll_openclaw_final(
        session_key,
        policy=policy,
        http_client=http_client or get_http_client(),
        trace_id=get_current_trace_id(),
        retry_count=retry_count,
    )


def _poll_update(record_id: int, **fields: Any) -> WebhookData:
    return {"id": record_id, "action": "update", **fields}


def _poll_skip(record_id: int) -> WebhookData:
    return {"id": record_id, "action": "skip"}


def _elapsed_since(started_at: datetime | None, *, default: float = 0.0) -> float:
    return (utcnow() - started_at).total_seconds() if started_at else default


async def _failure_update_with_notification(
    rec: WebhookData,
    update: WebhookData,
    reason: str,
    *,
    policy: OpenClawPollPolicy,
) -> WebhookData:
    record_id = rec["id"]
    await _clear_poll_stability(record_id)
    notify_dict = {**rec, **update}
    await send_deep_analysis_failure_notification(notify_dict, reason, policy=policy)
    return _poll_update(record_id, **update)


async def _handle_poll_timeout(
    rec: WebhookData,
    timeout_started_at: datetime | None,
    *,
    policy: OpenClawPollPolicy,
) -> WebhookData | None:
    if timeout_started_at is None:
        return None
    record_id = rec["id"]
    elapsed_total = _elapsed_since(timeout_started_at)
    timeout_seconds = policy.timeout_seconds
    if elapsed_total <= timeout_seconds:
        return None
    logger.info("[Poller] 分析超时: id=%s elapsed=%.0fs timeout=%ss", record_id, elapsed_total, timeout_seconds)
    DEEP_ANALYSIS_TOTAL.labels(status="timeout", engine=rec.get("engine", "openclaw")).inc()
    update: WebhookData = {"status": DeepAnalysisStatus.FAILED, "analysis_result": {"root_cause": "OpenClaw 分析超时"}}
    return await _failure_update_with_notification(rec, update, "超时失败", policy=policy)


async def _handle_missing_session_key(
    rec: WebhookData,
    timeout_started_at: datetime | None,
    *,
    policy: OpenClawPollPolicy,
) -> WebhookData | None:
    if rec["openclaw_session_key"]:
        return None
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    record_id = rec["id"]
    elapsed = _elapsed_since(timeout_started_at, default=999.0)
    if elapsed < compute_openclaw_poll_delay(0, policy=policy):
        return _poll_skip(record_id)
    logger.warning("[Poller] 缺少 session_key，标记失败: id=%s elapsed=%.0fs", record_id, elapsed)
    DEEP_ANALYSIS_TOTAL.labels(status="failed", engine=rec.get("engine", "openclaw")).inc()
    update: WebhookData = {
        "status": DeepAnalysisStatus.FAILED,
        "analysis_result": {
            "root_cause": "无法获取分析会话，OpenClaw 触发失败",
            "error": "missing_session_key",
            "failure_reason": "未能获取到分析会话密钥",
        },
    }
    return await _failure_update_with_notification(rec, update, "无 session_key - OpenClaw 触发失败", policy=policy)


async def _fetch_poll_result(rec: WebhookData, *, policy: OpenClawPollPolicy) -> WebhookData:
    if policy.has_http_api:
        return await poll_openclaw_result_via_http(rec["openclaw_session_key"], policy=policy)
    return await poll_session_result(
        gateway_url=policy.gateway_url,
        gateway_token=policy.gateway_token,
        session_key=rec["openclaw_session_key"],
        timeout=policy.poll_timeout_seconds,
    )


def extract_robust_json(text: str) -> str | None:
    if not isinstance(text, str):
        return None
    start_idx = text.find("{")
    if start_idx == -1:
        return None
    stack = 0
    for i in range(start_idx, len(text)):
        if text[i] == "{":
            stack += 1
        elif text[i] == "}":
            stack -= 1
            if stack == 0:
                return text[start_idx : i + 1]
    return None


def build_analysis_result_from_openclaw_text(text: str, run_id: str = "") -> WebhookData:
    parsed_result = None
    json_text = extract_robust_json(text)
    if json_text:
        try:
            parsed_result = json.loads(json_text)
        except json.JSONDecodeError:
            parsed_result = None
    if parsed_result and isinstance(parsed_result, dict):
        parsed_result["_openclaw_run_id"] = run_id
        parsed_result["_openclaw_text"] = text
        return dict(parsed_result)
    return {"root_cause": text, "_openclaw_text": text}


def _completed_update(rec: WebhookData, text: str, timeout_started_at: datetime | None) -> WebhookData:
    record_id = rec["id"]
    analysis_result = build_analysis_result_from_openclaw_text(text, str(rec["openclaw_run_id"] or ""))
    duration = _elapsed_since(timeout_started_at)
    DEEP_ANALYSIS_TOTAL.labels(status="completed", engine=rec.get("engine", "openclaw")).inc()
    return _poll_update(
        record_id,
        _need_success_notify=True,
        status=DeepAnalysisStatus.COMPLETED,
        analysis_result=analysis_result,
        duration_seconds=duration,
    )


def _poll_snapshot(text: str, msg_count: int) -> WebhookData:
    return {"msg_count": msg_count, "text_len": len(text), "text_hash": _text_hash(text)}


def _is_same_poll_snapshot(previous: WebhookData | None, current: WebhookData) -> bool:
    return bool(
        previous
        and previous.get("msg_count") == current["msg_count"]
        and previous.get("text_len") == current["text_len"]
        and previous.get("text_hash") == current["text_hash"]
    )


async def _handle_completed_poll_result(
    rec: WebhookData,
    result: WebhookData,
    timeout_started_at: datetime | None,
    *,
    policy: OpenClawPollPolicy,
) -> WebhookData:
    record_id = rec["id"]
    text = str(result.get("text", ""))
    msg_count = int(result.get("msg_count", 0) or 0)
    required_hits = 1 if result.get("is_final") is True else policy.stability_required_hits

    if required_hits <= 1:
        logger.info("[Poller] 分析完成，稳定命中阈值为 1，直接写库: id=%s", record_id)
        await _clear_poll_stability(record_id)
        return _completed_update(rec, text, timeout_started_at)

    current_snapshot = _poll_snapshot(text, msg_count)
    prev_snapshot = await _get_poll_stability(record_id)

    if _is_same_poll_snapshot(prev_snapshot, current_snapshot):
        hit_count = int(prev_snapshot.get("hit_count", 1) if prev_snapshot else 1) + 1
        logger.info(
            "[Poller] 结果稳定检查: id=%s hit=%s/%s msg_count=%s text_len=%s",
            record_id,
            hit_count,
            required_hits,
            msg_count,
            len(text),
        )
        if hit_count < required_hits:
            await _set_poll_stability(record_id, {**current_snapshot, "hit_count": hit_count})
            return _poll_skip(record_id)
        logger.info("[Poller] 分析稳定确认，准备写库: id=%s", record_id)
        await _clear_poll_stability(record_id)
        return _completed_update(rec, text, timeout_started_at)

    logger.info("[Poller] 首次或结果变化，等待稳定: id=%s msg_count=%s text_len=%s", record_id, msg_count, len(text))
    await _set_poll_stability(record_id, {**current_snapshot, "hit_count": 1, "first_result": {"text": text}})
    return _poll_skip(record_id)


async def _handle_error_poll_result(
    rec: WebhookData,
    result: WebhookData,
    *,
    policy: OpenClawPollPolicy,
) -> WebhookData:
    record_id = rec["id"]
    prev_snapshot = await _get_poll_stability(record_id)
    if prev_snapshot and "first_result" in prev_snapshot:
        error_count = int(prev_snapshot.get("error_count", 0) or 0) + 1
        if error_count >= policy.max_consecutive_errors and policy.enable_degradation:
            first_result = prev_snapshot.get("first_result", {})
            text = str(first_result.get("text", "")) if isinstance(first_result, dict) else ""
            logger.warning("[Poller] 连续错误达阈值，降级使用首次结果: id=%s error_count=%d", record_id, error_count)
            await _clear_poll_stability(record_id)
            DEEP_ANALYSIS_TOTAL.labels(status="degraded", engine=rec.get("engine", "openclaw")).inc()
            return _poll_update(
                record_id,
                status=DeepAnalysisStatus.COMPLETED,
                analysis_result=build_analysis_result_from_openclaw_text(text, str(rec["openclaw_run_id"] or "")),
            )
        await _set_poll_stability(record_id, {**prev_snapshot, "error_count": error_count})
        return _poll_skip(record_id)

    error_msg = str(result.get("error", "OpenClaw 返回错误"))
    if bool(result.get("retryable")) or _is_transient_poll_error(error_msg):
        logger.warning(
            "[Poller] OpenClaw 轮询遇到临时错误，保留 pending 等待下轮重试: id=%s error=%s", record_id, error_msg
        )
        return _poll_skip(record_id)

    DEEP_ANALYSIS_TOTAL.labels(status="failed", engine=rec.get("engine", "openclaw")).inc()
    update: WebhookData = {
        "status": DeepAnalysisStatus.FAILED,
        "analysis_result": {"root_cause": error_msg, "error": error_msg, "failure_reason": error_msg},
    }
    return await _failure_update_with_notification(rec, update, error_msg, policy=policy)


async def _handle_poll_result(
    rec: WebhookData,
    result: WebhookData,
    timeout_started_at: datetime | None,
    *,
    policy: OpenClawPollPolicy,
) -> WebhookData:
    status = result.get("status")
    if status == "completed":
        return await _handle_completed_poll_result(rec, result, timeout_started_at, policy=policy)
    if status == "error":
        return await _handle_error_poll_result(rec, result, policy=policy)
    logger.info(
        "[Poller] 分析仍在进行中: id=%s elapsed=%.0fs status=%s",
        rec["id"],
        _elapsed_since(timeout_started_at),
        status or "unknown",
    )
    return _poll_skip(rec["id"])


async def _poll_single_record(rec: WebhookData, *, policy: OpenClawPollPolicy | None = None) -> WebhookData:
    policy = policy or OpenClawPollPolicy.from_config()
    record_id = rec["id"]

    try:
        timeout_started_at = _poll_timeout_started_at(rec)
        timeout_result = await _handle_poll_timeout(rec, timeout_started_at, policy=policy)
        if timeout_result is not None:
            return timeout_result
        missing_session_result = await _handle_missing_session_key(rec, timeout_started_at, policy=policy)
        if missing_session_result is not None:
            return missing_session_result
        result = await _fetch_poll_result(rec, policy=policy)
        return await _handle_poll_result(rec, result, timeout_started_at, policy=policy)
    except Exception as e:
        logger.error("轮询记录 id=%s 失败: %s", record_id, e, exc_info=True)
        return {
            "id": record_id,
            "action": "update",
            "status": DeepAnalysisStatus.FAILED,
            "analysis_result": {
                "root_cause": f"分析任务崩溃: {e}",
                "error": str(e),
                "failure_reason": f"轮询异常: {e}",
            },
        }


def _record_to_poll_dict(record: Any) -> WebhookData:
    return {
        "id": record.id,
        "webhook_event_id": record.webhook_event_id,
        "engine": record.engine,
        "openclaw_session_key": record.openclaw_session_key,
        "openclaw_run_id": record.openclaw_run_id,
        "created_at": record.created_at,
        "status": record.status,
        "analysis_result": record.analysis_result,
        "duration_seconds": record.duration_seconds,
        "poll_attempts": record.poll_attempts,
        "last_polled_at": record.last_polled_at,
    }


async def _claim_openclaw_poll(
    analysis_id: int, *, policy: OpenClawPollPolicy | None = None
) -> tuple[WebhookData | None, int | None]:
    from sqlalchemy import select, update

    from db.session import session_scope
    from models import DeepAnalysis

    policy = policy or OpenClawPollPolicy.from_config()
    now = utcnow()
    lease_until = now + timedelta(seconds=_poll_claim_lease_seconds(policy))
    async with session_scope() as session:
        result = await session.execute(
            update(DeepAnalysis)
            .where(DeepAnalysis.id == analysis_id)
            .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            .where((DeepAnalysis.next_poll_at.is_(None)) | (DeepAnalysis.next_poll_at <= now))
            .values(poll_attempts=DeepAnalysis.poll_attempts + 1, last_polled_at=now, next_poll_at=lease_until)
            .returning(DeepAnalysis)
        )
        record = result.scalar_one_or_none()
        if record:
            return _record_to_poll_dict(record), None
        next_poll_at = (
            await session.execute(
                select(DeepAnalysis.next_poll_at)
                .where(DeepAnalysis.id == analysis_id)
                .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            )
        ).scalar_one_or_none()
        if next_poll_at and next_poll_at > now:
            return None, _seconds_until(next_poll_at)
    return None, None


async def _schedule_openclaw_poll_task(analysis_id: int, delay_seconds: int) -> None:
    try:
        from services.operations.taskiq_retry_scheduler import schedule_openclaw_poll

        await schedule_openclaw_poll(analysis_id, delay_seconds)
    except Exception as e:
        logger.warning("[Poller] OpenClaw 下次轮询调度失败 analysis_id=%s error=%s", analysis_id, e)


async def _schedule_next_openclaw_poll(
    analysis_id: int,
    poll_attempts: int,
    created_at: datetime | None,
    *,
    policy: OpenClawPollPolicy | None = None,
) -> None:
    from db.session import session_scope
    from models import DeepAnalysis
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    delay = _clamp_poll_delay_to_timeout(
        compute_openclaw_poll_delay(poll_attempts, policy=policy), created_at, policy=policy
    )
    next_poll_at = utcnow() + timedelta(seconds=delay)
    async with session_scope() as session:
        record = await session.get(DeepAnalysis, analysis_id)
        if not record or record.status != DeepAnalysisStatus.PENDING:
            return
        record.next_poll_at = next_poll_at
    await _schedule_openclaw_poll_task(analysis_id, delay)
    logger.info("[Poller] 已调度下次 OpenClaw 轮询 analysis_id=%s delay=%ss", analysis_id, delay)


async def poll_deep_analysis_once(analysis_id: int, *, policy: OpenClawPollPolicy | None = None) -> None:
    from db.session import session_scope
    from models import DeepAnalysis

    try:
        policy = policy or OpenClawPollPolicy.from_config()
        record_dict, early_reschedule_delay = await _claim_openclaw_poll(analysis_id, policy=policy)
        if early_reschedule_delay is not None:
            logger.debug(
                "[Poller] OpenClaw poll 任务提前触发，重新调度: id=%s delay=%ss", analysis_id, early_reschedule_delay
            )
            await _schedule_openclaw_poll_task(analysis_id, early_reschedule_delay)
            return
        if record_dict is None:
            logger.debug("[Poller] 没有可领取的 pending 分析: id=%s", analysis_id)
            return

        logger.info(
            "[Poller] 轮询 OpenClaw 分析: id=%s webhook_id=%s attempt=%s",
            analysis_id,
            record_dict.get("webhook_event_id"),
            record_dict.get("poll_attempts"),
        )

        poll_result = await _poll_single_record(record_dict, policy=policy)

        if poll_result.get("action") != "update":
            await _schedule_next_openclaw_poll(
                analysis_id,
                int(record_dict.get("poll_attempts") or 0),
                _poll_timeout_started_at(record_dict),
                policy=policy,
            )
            return

        async with session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(DeepAnalysis)
                .where(DeepAnalysis.id == analysis_id)
                .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            )
            record = result.scalar_one_or_none()
            if not record:
                return
            if "status" in poll_result:
                record.status = poll_result["status"]
            if "analysis_result" in poll_result:
                record.analysis_result = poll_result["analysis_result"]
            if "duration_seconds" in poll_result:
                record.duration_seconds = poll_result["duration_seconds"]
            record.next_poll_at = None
            await session.flush()

            if poll_result.get("_need_success_notify"):
                try:
                    from models import WebhookEvent

                    evt_stmt = select(WebhookEvent).filter_by(id=record_dict["webhook_event_id"])
                    evt_result = await session.execute(evt_stmt)
                    event = evt_result.scalars().first()
                    source = event.source if event else ""
                    notify_dict = {**record_dict, **poll_result}
                    asyncio.create_task(
                        _safe_notify(send_deep_analysis_success_notification(notify_dict, source, policy=policy))
                    )
                except Exception as e:
                    logger.debug("飞书深度分析通知失败: %s", e)
    except Exception as e:
        logger.error("[Poller] 轮询任务异常 analysis_id=%s error=%s", analysis_id, e, exc_info=True)


async def run_openclaw_poll_scan(limit: int = 100) -> int:
    from sqlalchemy import select

    from db.session import session_scope
    from models import DeepAnalysis

    now = utcnow()
    async with session_scope() as session:
        stmt = (
            select(DeepAnalysis.id)
            .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            .where((DeepAnalysis.next_poll_at.is_(None)) | (DeepAnalysis.next_poll_at <= now))
            .order_by(DeepAnalysis.next_poll_at.asc(), DeepAnalysis.id.asc())
            .limit(limit)
        )
        ids = list((await session.execute(stmt)).scalars().all())

    for analysis_id in ids:
        await _schedule_openclaw_poll_task(analysis_id, 0)
    if ids:
        logger.info("[Poller] 扫描调度 pending OpenClaw 分析 count=%s ids=%s", len(ids), ids)
    else:
        logger.debug("[Poller] 扫描未发现待调度 OpenClaw 分析")
    return len(ids)


# ═══════════════════════════════════════════════════════════════════════════════
# 转发触发
# ═══════════════════════════════════════════════════════════════════════════════


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _extract_openclaw_overview(source: str, alert_data: dict[str, Any]) -> dict[str, Any]:
    first_alert: dict[str, Any] = {}
    alerts = alert_data.get("alerts")
    if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict):
        first_alert = alerts[0]
    labels = _dict_or_empty(first_alert.get("labels"))
    annotations = _dict_or_empty(first_alert.get("annotations"))
    overview: dict[str, Any] = {
        "source": source,
        "type": alert_data.get("Type"),
        "rule_name": alert_data.get("RuleName") or labels.get("alertname") or alert_data.get("alertingRuleName"),
        "level": alert_data.get("Level") or labels.get("severity") or labels.get("internal_label_alert_level"),
        "summary": alert_data.get("summary") or annotations.get("summary") or annotations.get("description"),
    }
    if labels:
        overview["labels"] = labels
    if annotations:
        overview["annotations"] = annotations
    if first_alert:
        overview["prometheus_alert"] = {
            "status": first_alert.get("status"),
            "startsAt": first_alert.get("startsAt"),
            "endsAt": first_alert.get("endsAt"),
            "generatorURL": first_alert.get("generatorURL"),
            "fingerprint": first_alert.get("fingerprint") or labels.get("internal_label_alert_id"),
        }
    return {k: v for k, v in overview.items() if v not in (None, "", {}, [])}


def _build_openclaw_prompt_payload(source: str, alert_data: dict[str, Any]) -> dict[str, Any]:
    overview = _extract_openclaw_overview(source, alert_data)
    return {"overview": overview, "payload": alert_data}


async def analyze_with_openclaw(
    webhook_data: WebhookData,
    user_question: str = "",
    thinking_level: str = "high",
    *,
    policy: OpenClawTriggerPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
    dependencies: OpenClawForwardDependencies | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> ForwardResult:
    from core.observability.tracing import get_current_trace_id

    policy = policy or OpenClawTriggerPolicy.from_config()
    dependencies = dependencies or build_openclaw_forward_dependencies()
    if http_client is not None:
        dependencies = OpenClawForwardDependencies(
            http_client=http_client, circuit_breaker=dependencies.circuit_breaker
        )
    if not policy.enabled:
        logger.warning("[OpenClaw] 未启用，跳过深度分析")
        return {"_degraded": True, "_degraded_reason": "OpenClaw 未启用"}

    alert_data = webhook_data.get("parsed_data", {})
    source = webhook_data.get("source", "unknown")
    if not isinstance(alert_data, dict):
        alert_data = {"raw": alert_data}
    from services.webhooks.payload_sanitizer import sanitize_for_ai_async

    alert_data = await sanitize_for_ai_async(alert_data, strip_configured_keys=False, truncate=False)
    prompt_payload = _build_openclaw_prompt_payload(str(source), alert_data)
    template = await load_deep_analysis_prompt_template()

    overview_json = json.dumps(prompt_payload.get("overview", {}))
    payload_json = json.dumps(prompt_payload)
    message = (
        f"{template}\n\n"
        "## 当前告警关键字段（优先使用）\n"
        f"告警来源: {source}\n"
        "```json\n"
        f"{overview_json}\n"
        "```\n\n"
        "## 当前告警数据\n"
        "下面的 payload 仅做敏感字段脱敏，不做大小裁剪；若网关或模型显示层发生截断，请基于上方关键字段继续排查，不要要求用户重新粘贴。\n"
        "```json\n"
        f"{payload_json}\n"
        "```"
    )
    if user_question:
        message += f"\n\n## 用户补充问题\n{user_question}"
    logger.info(
        "[OpenClaw] 深度分析 prompt 已加载 source=%s bytes=%s",
        get_prompt_source(DEEP_ANALYSIS_PROMPT_KIND),
        len(template.encode("utf-8")),
    )

    session_key = f"hook:deep-analysis:{source}:{uuid.uuid4()}"
    payload = {
        "message": message,
        "name": "deep-analysis",
        "sessionKey": session_key,
        "wakeMode": "now",
        "deliver": False,
        "thinking": thinking_level,
        "timeoutSeconds": policy.timeout_seconds,
    }

    platform_name = policy.platform
    hooks_token = policy.hooks_token
    payload_bytes = json.dumps_bytes(payload)
    connect_timeout = policy.connect_timeout

    if platform_name == "hermes":
        target_url = f"{policy.gateway_url}/webhooks/agent"
        signature = hmac_mod.new(hooks_token.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        headers = {"Content-Type": _JSON_UTF8_CONTENT_TYPE, "X-Webhook-Signature": signature}
    else:
        target_url = f"{policy.gateway_url}/hooks/agent"
        headers = {"Authorization": f"Bearer {hooks_token}", "Content-Type": _JSON_UTF8_CONTENT_TYPE}
    kwargs: dict[str, Any] = {"content": payload_bytes}

    trace_id = get_current_trace_id()
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    if not hooks_token:
        logger.warning("[%s] OpenClaw token 为空，将按当前配置继续发起请求", platform_name.upper())
    logger.info(
        "[%s] 正在发起分析请求: target=%s session_key=%s payload_bytes=%s trace_id=%s",
        platform_name.upper(),
        mask_url(target_url),
        session_key,
        len(payload_bytes),
        trace_id or "-",
    )

    max_retries = policy.max_retries
    last_error = None
    response: httpx.Response | None = None

    for attempt in range(max_retries):
        try:
            response = cast(
                httpx.Response,
                await dependencies.circuit_breaker.call_async(
                    dependencies.http_client.post,
                    target_url,
                    headers=headers,
                    timeout=httpx.Timeout(60.0, connect=connect_timeout),
                    **kwargs,
                ),
            )
            response.raise_for_status()
            break
        except CircuitBreakerOpenException as e:
            last_error = str(e)
            logger.warning("[%s] 请求被熔断器拦截 target=%s error=%s", platform_name.upper(), mask_url(target_url), e)
            if policy.enable_degradation:
                return {"_degraded": True, "_degraded_reason": f"{platform_name.capitalize()} 请求失败: {last_error}"}
            raise
        except Exception as e:
            last_error = str(e)
            logger.warning(
                "[%s] 请求异常 target=%s attempt=%d/%d error_type=%s error=%s",
                platform_name.upper(),
                mask_url(target_url),
                attempt + 1,
                max_retries,
                type(e).__name__,
                e,
            )
            if attempt < max_retries - 1:
                await (sleep or asyncio.sleep)(policy.retry_sleep_seconds)
    else:
        logger.error("[%s] 请求失败，已重试 %d 次: %s", platform_name.upper(), max_retries, last_error)
        if policy.enable_degradation:
            return {"_degraded": True, "_degraded_reason": f"{platform_name.capitalize()} 请求失败: {last_error}"}
        raise Exception(f"{platform_name.capitalize()} 请求失败: {last_error}")

    if response is None:
        raise RuntimeError(f"{platform_name.capitalize()} 请求失败: empty response")

    try:
        raw = response.json()
        if not isinstance(raw, dict):
            raise ValueError("OpenClaw response is not a JSON object")
        result: dict[str, Any] = raw
        if platform_name == "hermes":
            run_id = result.get("delivery_id") or result.get("runId")
            session_key = run_id if run_id else session_key
        else:
            run_id = result.get("runId")
        logger.info(
            "[%s] 成功触发深度分析 run_id=%s session_key=%s status_code=%s",
            platform_name.upper(),
            run_id,
            session_key,
            response.status_code,
        )
        return {"_pending": True, "_openclaw_run_id": run_id, "_openclaw_session_key": session_key}
    except Exception as e:
        logger.error("[OpenClaw] 响应解析失败 status_code=%s error=%s", response.status_code, e)
        if policy.enable_degradation:
            return {"_degraded": True, "_degraded_reason": f"响应解析失败: {e!s}"}
        raise


async def forward_to_openclaw(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    *,
    policy: OpenClawTriggerPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
    dependencies: OpenClawForwardDependencies | None = None,
) -> ForwardResult:
    started = time.perf_counter()
    status = "unknown"
    policy = policy or OpenClawTriggerPolicy.from_config()
    dependencies = dependencies or build_openclaw_forward_dependencies()
    if http_client is not None:
        dependencies = OpenClawForwardDependencies(
            http_client=http_client, circuit_breaker=dependencies.circuit_breaker
        )
    if not policy.enabled:
        logger.debug("[Forward] OpenClaw 未启用，跳过深度分析")
        status = "disabled"
        FORWARD_DELIVERY_TOTAL.labels("openclaw", status).inc()
        FORWARD_DELIVERY_DURATION_SECONDS.labels("openclaw", status).observe(time.perf_counter() - started)
        return {"status": "disabled"}

    async def _do_request() -> ForwardResult:
        from services.analysis.ai_analyzer import analyze_webhook_with_ai

        result = await analyze_with_openclaw(webhook_data, policy=policy, dependencies=dependencies)
        if result.get("_degraded"):
            logger.warning("[Forward] OpenClaw 降级，回退本地 AI: %s", result.get("_degraded_reason"))
            local_data = {
                "source": webhook_data.get("source", "unknown"),
                "headers": webhook_data.get("headers", {}),
                "parsed_data": webhook_data.get("parsed_data", {}),
            }
            return cast(ForwardResult, await analyze_webhook_with_ai(local_data))
        return result

    try:
        res = await dependencies.circuit_breaker.call_async(_do_request)
        status = str(res.get("status") or ("pending" if res.get("_pending") else "success"))
        return res
    except CircuitBreakerOpenException:
        status = "circuit_broken"
        return {"status": "circuit_broken"}
    except Exception as e:
        logger.error("OpenClaw 转发异常: %s", e)
        status = "error"
        return {"status": "error", "message": str(e)}
    finally:
        FORWARD_DELIVERY_TOTAL.labels("openclaw", status).inc()
        FORWARD_DELIVERY_DURATION_SECONDS.labels("openclaw", status).observe(time.perf_counter() - started)
