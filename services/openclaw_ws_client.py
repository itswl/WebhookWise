"""
OpenClaw WebSocket 客户端模块（异步版）

提供两类能力：
- poll_session_result：基于 chat.history 的短连接轮询（sessionKey → 最终文本）
- wait_for_result：监听 runId 的事件流（agent/chat 事件 → 最终文本）
"""

import asyncio
import base64
import json
import platform
import time
import uuid

import websockets

from core.config import Config
from core.logger import get_logger

logger = get_logger("openclaw_ws")


def _http_to_ws_url(http_url: str) -> str:
    url = http_url.rstrip("/")
    if url.startswith("https://"):
        return url.replace("https://", "wss://") + "/ws"
    if url.startswith("http://"):
        return url.replace("http://", "ws://") + "/ws"
    return f"ws://{url}/ws"


def _build_connect_frame(token: str, device_auth: dict | None = None) -> dict:
    client_platform = "linux" if device_auth else platform.system().lower()
    client_mode = "cli" if device_auth else "backend"

    frame = {
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


def _build_device_auth(nonce: str) -> dict | None:
    device_id = Config.openclaw.OPENCLAW_DEVICE_ID
    private_key_b64 = Config.openclaw.OPENCLAW_DEVICE_PRIVATE_KEY_PEM
    device_token = Config.openclaw.OPENCLAW_DEVICE_TOKEN

    if not device_id or not private_key_b64:
        return None

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    except ImportError:
        logger.warning("cryptography package not installed, skipping device auth")
        return None

    try:
        pem = f"-----BEGIN PRIVATE KEY-----\n{private_key_b64}\n-----END PRIVATE KEY-----\n"
        private_key = serialization.load_pem_private_key(pem.encode(), password=None)

        pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        pub_b64url = base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")

        signed_at = int(time.time() * 1000)
        gateway_token = Config.openclaw.OPENCLAW_GATEWAY_TOKEN
        scopes_str = "operator.read"
        payload = f"v2|{device_id}|gateway-client|cli|operator|{scopes_str}|{signed_at}|{gateway_token}|{nonce}"

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
    except Exception as e:
        logger.warning("Failed to build device auth: %s", e)
        return None


async def _try_recv_challenge(ws, timeout: float | None = None) -> str | None:
    if timeout is None:
        timeout = Config.openclaw.OPENCLAW_NONCE_TIMEOUT
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        frame = json.loads(raw)
        if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
            nonce = frame.get("payload", {}).get("nonce", "")
            if nonce:
                logger.info("Received connect.challenge, nonce=%s...", nonce[:16])
                return nonce
    except asyncio.TimeoutError:
        return None
    except Exception as e:
        logger.debug("Error receiving challenge: %s", e)
        return None
    return None


async def _handshake(ws, gateway_token: str, timeout: float) -> tuple[bool, str | None]:
    try:
        nonce = await _try_recv_challenge(ws)
        device_auth = _build_device_auth(nonce) if nonce else None
        connect_frame = _build_connect_frame(gateway_token, device_auth=device_auth)
        await ws.send(json.dumps(connect_frame))

        response = None
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            frame = json.loads(raw)
            if frame.get("type") == "res":
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
    except Exception:
        return False, "handshake_error"


def _parse_history_messages(messages: list) -> dict:
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


async def poll_session_result(gateway_url: str, gateway_token: str, session_key: str, timeout: int = 30) -> dict:
    ws_url = _http_to_ws_url(gateway_url)
    start = time.monotonic()

    connect_timeout = min(5, max(1, timeout // 3))
    handshake_timeout = min(15, max(3, timeout // 2))

    try:
        async with websockets.connect(
            ws_url,
            open_timeout=connect_timeout,
            close_timeout=1,
            max_size=None,
        ) as ws:
            ok, err_type = await _handshake(ws, gateway_token, timeout=handshake_timeout)
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
                frame = json.loads(raw)
                if frame.get("type") != "res" or frame.get("id") != request_id:
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
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def wait_for_result(
    gateway_url: str, gateway_token: str, run_id: str, timeout: int = 300, connect_timeout: int | None = None
) -> dict:
    ws_url = _http_to_ws_url(gateway_url)
    connect_timeout = connect_timeout or Config.openclaw.OPENCLAW_CONNECT_TIMEOUT
    handshake_timeout = Config.openclaw.OPENCLAW_HANDSHAKE_TIMEOUT

    text_fragments: list[str] = []

    try:
        async with websockets.connect(
            ws_url,
            open_timeout=connect_timeout,
            close_timeout=1,
            max_size=None,
        ) as ws:
            ok, err_type = await _handshake(ws, gateway_token, timeout=handshake_timeout)
            if not ok:
                return {"status": "error", "run_id": run_id, "error": err_type or "handshake_failed"}

            async def _recv_loop():
                while True:
                    raw = await ws.recv()
                    frame = json.loads(raw)
                    if frame.get("type") != "event":
                        continue
                    payload = frame.get("payload", {}) or {}
                    if payload.get("runId") != run_id:
                        continue
                    event_type = frame.get("event")

                    if event_type == "agent":
                        if payload.get("stream") == "assistant":
                            data = payload.get("data")
                            text = ""
                            if isinstance(data, dict):
                                text = data.get("text", "") or data.get("delta", "")
                            elif isinstance(data, str):
                                text = data
                            if text:
                                text_fragments.append(text)
                        continue

                    if event_type == "chat":
                        state = payload.get("state")
                        if state == "error":
                            error_msg = payload.get("errorMessage", "Unknown error")
                            return {"status": "error", "run_id": run_id, "error": error_msg}
                        if state == "final":
                            message = payload.get("message", {}) or {}
                            content = message.get("content", [])
                            final_text = ""
                            if isinstance(content, list):
                                text_parts = [
                                    item.get("text", "")
                                    for item in content
                                    if isinstance(item, dict) and item.get("type") == "text"
                                ]
                                final_text = "\n".join([t for t in text_parts if t])
                            elif isinstance(content, str):
                                final_text = content
                            if not final_text:
                                final_text = "".join([t for t in text_fragments if isinstance(t, str)])
                            return {"status": "success", "run_id": run_id, "message": message, "text": final_text}

            return await asyncio.wait_for(_recv_loop(), timeout=timeout)

    except asyncio.TimeoutError:
        partial_text = "".join([t for t in text_fragments if isinstance(t, str)])
        return {"status": "timeout", "run_id": run_id, "partial_text": partial_text}
    except Exception as e:
        return {"status": "error", "run_id": run_id, "error": str(e)}

    return {"status": "timeout", "run_id": run_id, "partial_text": "".join(text_fragments)}
