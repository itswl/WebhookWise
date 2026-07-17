"""OpenClaw HTTP and WebSocket clients."""

from __future__ import annotations

import asyncio
import base64
import platform
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import websockets

from contracts.webhook_payload import JsonObject
from core import json
from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.logger import get_logger

logger = get_logger("openclaw.client")


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
    stability_ttl_seconds: int
    max_consecutive_errors: int
    enable_degradation: bool
    notification_webhook_url: str

    @classmethod
    def from_config(cls) -> OpenClawPollPolicy:
        cfg = get_config_manager()
        return cls(
            timeout_seconds=int(cfg.openclaw.OPENCLAW_TIMEOUT_SECONDS),
            poll_timeout_seconds=max(1, int(cfg.openclaw.OPENCLAW_POLL_TIMEOUT_SECONDS)),
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
            connect_timeout_seconds=max(1.0, float(cfg.openclaw.OPENCLAW_CONNECT_TIMEOUT_SECONDS)),
            stability_required_hits=max(1, int(cfg.openclaw.OPENCLAW_STABILITY_REQUIRED_HITS)),
            stability_ttl_seconds=max(60, int(cfg.openclaw.OPENCLAW_POLL_STABILITY_TTL_SECONDS)),
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
    max_history_frames: int
    max_message_bytes: int = 2_097_152

    @classmethod
    def from_config(cls) -> OpenClawWsPolicy:
        cfg = get_config_manager()
        return cls(
            device_id=str(cfg.openclaw.OPENCLAW_DEVICE_ID),
            device_private_key_b64=str(cfg.openclaw.OPENCLAW_DEVICE_PRIVATE_KEY_PEM),
            device_token=str(cfg.openclaw.OPENCLAW_DEVICE_TOKEN),
            gateway_token=str(cfg.openclaw.OPENCLAW_GATEWAY_TOKEN),
            nonce_timeout=float(cfg.openclaw.OPENCLAW_NONCE_TIMEOUT_SECONDS),
            max_history_frames=max(1, int(cfg.openclaw.OPENCLAW_WS_MAX_HISTORY_FRAMES)),
            max_message_bytes=max(1, int(cfg.openclaw.OPENCLAW_WS_MAX_MESSAGE_BYTES)),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP polling
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
) -> JsonObject:
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
            logger.debug("HTTP /final request (attempt %s/%s): %s", attempt + 1, retry_count, url)
            response = await http_client.get(url, headers=headers, timeout=timeout)
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if response.status_code == 404:
                last_error = "Session not found"
                logger.warning("Session not found (attempt %d/%d elapsed=%sms)", attempt + 1, retry_count, elapsed_ms)
                continue
            if response.status_code in (202, 204):
                last_error = "analysis in progress"
                logger.debug("Analysis in progress (attempt %s/%s elapsed=%sms)", attempt + 1, retry_count, elapsed_ms)
                continue
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    "HTTP /final returned non-200 status=%s attempt=%s/%s elapsed=%sms",
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
                    "HTTP /final returned invalid JSON (attempt %s/%s elapsed=%sms)",
                    attempt + 1,
                    retry_count,
                    elapsed_ms,
                )
                continue
            if not isinstance(raw, dict):
                last_error = "Invalid JSON response"
                continue

            data: JsonObject = raw
            is_final = data.get("isFinal")
            is_processing = data.get("isProcessing", False)
            text = data.get("text", "")
            msg_count = int(data.get("messageCount", 0) or 0)

            if is_processing is True:
                last_error = "analysis in progress"
                continue
            if text and is_final is not False:
                result: JsonObject = {"status": "completed", "text": text, "msg_count": msg_count}
                if is_final is True:
                    result["is_final"] = True
                return result
            if is_final is False or not is_final:
                last_error = "analysis in progress"
                continue
            last_error = "No text content"
        except httpx.ReadTimeout as e:
            last_error = f"ReadTimeout after {policy.http_poll_timeout:g}s"
            logger.info(
                "HTTP /final wait timed out, treating as pending attempt=%s/%s timeout=%ss error_type=%s error=%s",
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
                "HTTP polling timed out attempt=%s/%s error_type=%s error=%s",
                attempt + 1,
                retry_count,
                type(e).__name__,
                last_error,
            )
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
            transport_error = True
            last_error = _describe_exception(e)
            logger.warning(
                "HTTP polling error attempt=%s/%s error_type=%s error=%s",
                attempt + 1,
                retry_count,
                type(e).__name__,
                last_error,
            )

    if last_error == "analysis in progress":
        return {"status": "pending"}
    if transport_error:
        return {"status": "error", "error": last_error or "HTTP transport error", "retryable": True}
    return {"status": "error", "error": last_error}


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket client
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
    except TimeoutError:
        return None
    except (OSError, RuntimeError, websockets.WebSocketException) as e:
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
    except TimeoutError:
        return False, "handshake_timeout"
    except json.JSONDecodeError:
        return False, "invalid_response"
    except (EOFError, OSError, RuntimeError, websockets.WebSocketException):
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
        async with websockets.connect(
            ws_url,
            open_timeout=connect_timeout,
            close_timeout=1,
            max_size=max(1, int(getattr(policy, "max_message_bytes", 2_097_152))),
        ) as ws:
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

            for _ in range(policy.max_history_frames):
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
    except TimeoutError:
        return {"status": "error", "error": f"Timeout ({timeout}s)"}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"Invalid JSON response: {e}"}
    except (EOFError, OSError, RuntimeError, websockets.WebSocketException) as e:
        return {"status": "error", "error": str(e)}
