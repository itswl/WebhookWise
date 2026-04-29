"""
OpenClaw WebSocket 客户端模块

同步连接 OpenClaw Gateway WebSocket，等待指定 runId 的分析结果。
协议参考: openclaw-main/src/pkg/gateway/protocol/frames.go
"""

import base64
import json
import platform
import threading
import time
import uuid

import websocket

from core.config import Config
from core.logger import get_logger

logger = get_logger("openclaw_ws")


def _http_to_ws_url(http_url: str) -> str:
    """将 HTTP URL 转换为 WebSocket URL"""
    url = http_url.rstrip("/")
    if url.startswith("https://"):
        return url.replace("https://", "wss://") + "/ws"
    elif url.startswith("http://"):
        return url.replace("http://", "ws://") + "/ws"
    else:
        # 假设是无协议的地址
        return f"ws://{url}/ws"


def _build_connect_frame(token: str, device_auth: dict | None = None) -> dict:
    """构建 WebSocket 握手请求帧

    Args:
        token: Gateway 认证 token
        device_auth: OpenClaw 设备认证参数（可选），由 _build_device_auth 返回
    """
    # 如果有设备认证，platform 必须为 linux、mode 为 cli（匹配已配对设备）
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

    # 添加 OpenClaw 设备认证字段
    if device_auth:
        params = frame["params"]
        params["role"] = device_auth["role"]
        params["scopes"] = device_auth["scopes"]
        params["auth"]["deviceToken"] = device_auth["device_token"]
        params["device"] = device_auth["device"]
        logger.debug(f"Device auth attached: deviceId={device_auth['device']['id'][:16]}...")

    return frame


def _build_device_auth(nonce: str) -> dict | None:
    """构造 OpenClaw 设备认证参数（Ed25519 签名）

    认证流程：
    1. 从环境变量读取设备 ID、私钥 PEM、设备 token
    2. 用 Ed25519 私钥对 v2 签名 payload 签名
    3. 返回 role、scopes、device 等字段，供 connect frame 使用

    v2 签名 payload 格式（字段用 | 分隔）：
    v2|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}|{nonce}

    Args:
        nonce: 从 connect.challenge 事件中提取的随机数
    Returns:
        设备认证参数字典，或 None（未配置/签名失败）
    """
    device_id = Config.openclaw.OPENCLAW_DEVICE_ID
    private_key_b64 = Config.openclaw.OPENCLAW_DEVICE_PRIVATE_KEY_PEM
    device_token = Config.openclaw.OPENCLAW_DEVICE_TOKEN

    if not device_id or not private_key_b64:
        return None

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    except ImportError:
        logger.warning(
            "cryptography package not installed, skipping device auth. "
            "Install with: pip install cryptography>=42.0.0"
        )
        return None

    try:
        # 从 base64 编码的私钥数据重建完整 PEM
        pem = f"-----BEGIN PRIVATE KEY-----\n{private_key_b64}\n-----END PRIVATE KEY-----\n"
        private_key = serialization.load_pem_private_key(pem.encode(), password=None)

        # 获取 raw public key 并 base64url 编码（无 padding）
        pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        pub_b64url = base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")

        # 构造 v2 签名 payload
        signed_at = int(time.time() * 1000)
        gateway_token = Config.openclaw.OPENCLAW_GATEWAY_TOKEN
        scopes_str = "operator.read"
        payload = f"v2|{device_id}|gateway-client|cli|operator|{scopes_str}|{signed_at}|{gateway_token}|{nonce}"

        # Ed25519 签名，base64url 编码（无 padding）
        signature = private_key.sign(payload.encode())
        sig_b64url = base64.urlsafe_b64encode(signature).decode().rstrip("=")

        logger.debug(f"Device auth built: signedAt={signed_at}, nonce={nonce[:16]}...")

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
        logger.warning(f"Failed to build device auth: {e}")
        return None


def _try_recv_challenge(ws, timeout: float | None = None) -> str | None:
    if timeout is None:
        timeout = Config.openclaw.OPENCLAW_NONCE_TIMEOUT
    """尝试接收 connect.challenge 帧并提取 nonce

    OpenClaw Gateway 在 WebSocket 连接建立后、客户端发送 connect 之前，
    会主动推送一个 connect.challenge 事件帧，包含用于签名的 nonce。

    Args:
        ws: WebSocket 连接对象
        timeout: 接收超时（秒）
    Returns:
        nonce 字符串，或 None（非 OpenClaw / 超时）
    """
    old_timeout = ws.gettimeout()
    try:
        ws.settimeout(timeout)
        raw = ws.recv()
        frame = json.loads(raw)

        # connect.challenge 帧格式: {"type": "event", "event": "connect.challenge", "payload": {"nonce": "..."}}
        if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
            nonce = frame.get("payload", {}).get("nonce", "")
            if nonce:
                logger.info(f"Received connect.challenge, nonce={nonce[:16]}...")
                return nonce
            else:
                logger.warning("connect.challenge frame has no nonce")
        else:
            logger.debug(
                f"First frame is not connect.challenge: type={frame.get('type')}, event={frame.get('event', '')}"
            )
    except websocket.WebSocketTimeoutException:
        logger.debug("No connect.challenge received (timeout) — likely OpenClaw, not OpenClaw")
    except Exception as e:
        logger.debug(f"Error receiving challenge: {e}")
    finally:
        ws.settimeout(old_timeout)
    return None


# 连接相关常量（从 Config 读取，保留模块级默认值作为后备）
CONNECT_TIMEOUT = Config.openclaw.OPENCLAW_CONNECT_TIMEOUT  # TCP + WebSocket 握手超时（秒）
HANDSHAKE_TIMEOUT = Config.openclaw.OPENCLAW_HANDSHAKE_TIMEOUT  # OpenClaw 协议握手超时（秒）
RECV_TIMEOUT = Config.openclaw.OPENCLAW_RECV_TIMEOUT  # recv 超时，用于检查 _done 事件


class OpenClawWSClient:
    """OpenClaw WebSocket 客户端（线程安全，每次调用独立连接）"""

    def __init__(
        self, gateway_url: str, gateway_token: str, run_id: str, timeout: int = 300, connect_timeout: int | None = None
    ):
        self.ws_url = _http_to_ws_url(gateway_url)
        self.gateway_token = gateway_token
        self.run_id = run_id
        self.timeout = timeout  # 等待分析结果的超时
        self.connect_timeout = connect_timeout or CONNECT_TIMEOUT  # TCP + WS 握手超时

        # 结果存储
        self._result: dict | None = None
        self._text_fragments: list[str] = []
        self._lock = threading.Lock()
        self._done = threading.Event()

        # WebSocket 连接
        self._ws: websocket.WebSocket | None = None
        self._connection_error: str | None = None  # 记录连接错误类型

    def _send_connect(self) -> bool:
        """发送握手请求并验证响应

        流程（兼容 OpenClaw 和 OpenClaw）：
        1. 先尝试 recv() 接收 connect.challenge 帧 → 提取 nonce
        2. 如果有 nonce + 设备配置，构造带设备认证的 connect frame
        3. 否则构造普通 connect frame（向后兼容 OpenClaw）
        4. send(connect_frame)
        5. 循环 recv() 等待 type=res 的响应（跳过 event 帧）
        """
        logger.debug(f"Sending connect frame for runId={self.run_id}")

        try:
            # Step 1: 尝试接收 connect.challenge（OpenClaw 会在连接后立即推送）
            nonce = _try_recv_challenge(self._ws)

            # Step 2: 构造 connect frame（带或不带设备认证）
            device_auth = None
            if nonce:
                device_auth = _build_device_auth(nonce)
                if device_auth:
                    logger.info("Using OpenClaw device auth for connect handshake")
                else:
                    logger.debug("Challenge received but device auth not configured, using simple connect")

            connect_frame = _build_connect_frame(self.gateway_token, device_auth=device_auth)

            # 临时设置较短的超时用于握手
            self._ws.settimeout(HANDSHAKE_TIMEOUT)

            # Step 3: 发送 connect frame
            self._ws.send(json.dumps(connect_frame))

            # Step 4: 等待握手响应（跳过 event 帧）
            response = None
            for _ in range(5):
                response_raw = self._ws.recv()
                response = json.loads(response_raw)
                if response.get("type") == "res":
                    break
                logger.debug(
                    f"Skipping non-res frame during handshake: type={response.get('type')}, event={response.get('event', '')}"
                )

            if not response or response.get("type") != "res":
                self._connection_error = "auth_protocol_error"
                logger.error(f"Unexpected response type after retries: {response.get('type') if response else 'none'}")
                return False

            if not response.get("ok"):
                error = response.get("error", {})
                self._connection_error = "auth_failed"
                logger.error(f"Connect failed: {error.get('message', 'Unknown error')}")
                return False

            payload = response.get("payload", {})
            if payload.get("type") != "hello-ok":
                self._connection_error = "auth_protocol_error"
                logger.error(f"Unexpected payload type: {payload.get('type')}")
                return False

            logger.info(f"Connected to OpenClaw Gateway, connId={payload.get('server', {}).get('connId', 'unknown')}")
            return True

        except websocket.WebSocketTimeoutException:
            self._connection_error = "handshake_timeout"
            logger.error(f"OpenClaw handshake timeout ({HANDSHAKE_TIMEOUT}s) - server may be overloaded")
            return False
        except json.JSONDecodeError as e:
            self._connection_error = "invalid_response"
            logger.error(f"Failed to parse connect response: {e}")
            return False
        except Exception as e:
            self._connection_error = "handshake_error"
            logger.error(f"Connect handshake failed: {e}")
            return False

    def _process_event(self, frame: dict) -> bool:
        """
        处理事件帧，返回是否完成（True=已获得最终结果或错误）
        """
        event_type = frame.get("event")
        payload = frame.get("payload", {})
        frame_run_id = payload.get("runId")

        # 检查 runId 是否匹配
        if frame_run_id != self.run_id:
            return False

        if event_type == "agent":
            # 流式文本片段
            stream = payload.get("stream")
            if stream == "assistant":
                data = payload.get("data")
                # 兼容处理：data 可能是 dict 或 str
                if isinstance(data, dict):
                    text = data.get("text", "") or data.get("delta", "")
                elif isinstance(data, str):
                    text = data
                else:
                    text = ""
                if text:
                    with self._lock:
                        self._text_fragments.append(text)
                    logger.debug(f"Received text fragment, length={len(text)}")
            return False

        elif event_type == "chat":
            state = payload.get("state")

            if state == "final":
                # 分析完成
                message = payload.get("message", {})

                # 优先从 message.content 中提取最终文本（避免流式碎片重复问题）
                final_text = ""
                content = message.get("content", [])
                if isinstance(content, list):
                    # 提取 type=="text" 的内容，排除 type=="thinking"
                    text_parts = [
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    final_text = "\n".join(text_parts)
                elif isinstance(content, str):
                    final_text = content

                # 如果 message.content 提取失败，降级使用流式碎片
                if not final_text:
                    with self._lock:
                        fragments = [f for f in self._text_fragments if isinstance(f, str)]
                        final_text = "".join(fragments)

                with self._lock:
                    self._result = {
                        "status": "success",
                        "run_id": self.run_id,
                        "message": message,
                        "text": final_text,
                    }
                logger.info(f"Analysis completed for runId={self.run_id}")
                return True

            elif state == "error":
                # 分析失败
                error_msg = payload.get("errorMessage", "Unknown error")
                with self._lock:
                    self._result = {"status": "error", "run_id": self.run_id, "error": error_msg}
                logger.error(f"Analysis failed for runId={self.run_id}: {error_msg}")
                return True

        return False

    def _listen_loop(self):
        """WebSocket 消息监听循环"""
        try:
            while not self._done.is_set():
                try:
                    raw_message = self._ws.recv()
                    if not raw_message:
                        continue

                    frame = json.loads(raw_message)
                    frame_type = frame.get("type")

                    # 只处理事件帧
                    if frame_type == "event" and self._process_event(frame):
                        self._done.set()
                        return

                except websocket.WebSocketTimeoutException:
                    # recv 超时，继续循环检查 _done
                    continue
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse WebSocket message: {e}")
                    continue

        except websocket.WebSocketConnectionClosedException:
            logger.warning("WebSocket connection closed unexpectedly")
        except Exception as e:
            logger.error(f"Error in listen loop: {e}")

    def wait_for_result(self) -> dict:
        """
        连接 WebSocket 并等待分析结果

        返回值：
        - 成功: {"status": "success", "run_id": "...", "message": {...}, "text": "完整文本"}
        - 失败: {"status": "error", "run_id": "...", "error": "错误信息"}
        - 超时: {"status": "timeout", "run_id": "...", "partial_text": "已收集的部分文本"}
        """
        logger.info(
            f"Connecting to {self.ws_url} for runId={self.run_id}, connect_timeout={self.connect_timeout}s, result_timeout={self.timeout}s"
        )

        try:
            # 创建 WebSocket 连接（使用较短的连接超时快速失败）
            logger.debug(f"Establishing TCP + WebSocket connection to {self.ws_url}...")
            self._ws = websocket.create_connection(
                self.ws_url,
                timeout=self.connect_timeout,  # 连接超时，不是等待结果超时
                skip_utf8_validation=True,
            )
            logger.debug("WebSocket connection established, starting OpenClaw handshake...")

            # 发送 OpenClaw 握手
            if not self._send_connect():
                error_msg = self._get_connection_error_message()
                return {
                    "status": "error",
                    "run_id": self.run_id,
                    "error": error_msg,
                    "error_type": self._connection_error or "handshake_failed",
                }

            # 握手成功后，设置较短的 recv 超时用于监听循环
            self._ws.settimeout(RECV_TIMEOUT)

            # 启动监听线程
            listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
            listen_thread.start()

            # 等待结果或超时
            completed = self._done.wait(timeout=self.timeout)

            if completed and self._result:
                return self._result
            else:
                # 超时
                with self._lock:
                    # 类型安全检查：确保只 join 字符串类型
                    fragments = [f for f in self._text_fragments if isinstance(f, str)]
                    partial_text = "".join(fragments)
                logger.warning(f"Timeout waiting for runId={self.run_id}, collected {len(partial_text)} chars")
                return {"status": "timeout", "run_id": self.run_id, "partial_text": partial_text}

        except websocket.WebSocketTimeoutException:
            self._connection_error = "connect_timeout"
            logger.error(f"WebSocket connection timeout ({self.connect_timeout}s) - TCP or WS handshake failed")
            return {
                "status": "error",
                "run_id": self.run_id,
                "error": f"Connection timeout ({self.connect_timeout}s): Unable to establish WebSocket connection. Check network/firewall.",
                "error_type": "connect_timeout",
            }
        except ConnectionRefusedError as e:
            self._connection_error = "connection_refused"
            logger.error(f"Connection refused: {e}")
            return {
                "status": "error",
                "run_id": self.run_id,
                "error": f"Connection refused: Server at {self.ws_url} is not accepting connections.",
                "error_type": "connection_refused",
            }
        except OSError as e:
            # 网络层错误（DNS、路由等）
            self._connection_error = "network_error"
            logger.error(f"Network error: {e}")
            return {
                "status": "error",
                "run_id": self.run_id,
                "error": f"Network error: {e}",
                "error_type": "network_error",
            }
        except websocket.WebSocketException as e:
            self._connection_error = "websocket_error"
            logger.error(f"WebSocket error: {e}")
            return {
                "status": "error",
                "run_id": self.run_id,
                "error": f"WebSocket connection failed: {e}",
                "error_type": "websocket_error",
            }
        except Exception as e:
            self._connection_error = "unexpected_error"
            logger.error(f"Unexpected error: {e}")
            return {"status": "error", "run_id": self.run_id, "error": str(e), "error_type": "unexpected_error"}
        finally:
            # 确保清理
            self._done.set()
            if self._ws:
                try:
                    self._ws.close()
                except Exception as e:
                    logger.debug(f"WebSocket close failed: {e}")

    def _get_connection_error_message(self) -> str:
        """根据连接错误类型返回清晰的错误信息"""
        error_messages = {
            "connect_timeout": f"Connection timeout ({self.connect_timeout}s): Unable to reach server.",
            "handshake_timeout": f"OpenClaw handshake timeout ({HANDSHAKE_TIMEOUT}s): Server may be overloaded.",
            "auth_failed": "Authentication failed: Invalid gateway token.",
            "auth_protocol_error": "Protocol error: Unexpected response from server.",
            "invalid_response": "Invalid response: Failed to parse server response.",
            "handshake_error": "Handshake error: Failed to complete OpenClaw handshake.",
        }
        return error_messages.get(self._connection_error, "WebSocket connection failed")


def poll_session_result(gateway_url: str, gateway_token: str, session_key: str, timeout: int = 30) -> dict:
    """
    短连接轮询：连接 WS -> 握手 -> 调用 chat.history -> 提取结果 -> 断开
    整个过程 < 15 秒

    Args:
        gateway_url: OpenClaw Gateway HTTP URL (如 http://127.0.0.1:18900)
        gateway_token: Gateway 认证 token (OPENCLAW_GATEWAY_TOKEN)
        session_key: 会话 Key (如 "agent:main:employee:xxx:run:yyy")
        timeout: 整体超时时间（秒），默认 15

    Returns:
        - 分析完成: {"status": "completed", "text": "...", "message": {...}}
        - 仍在进行: {"status": "pending"}
        - 错误: {"status": "error", "error": "..."}
    """
    ws_url = _http_to_ws_url(gateway_url)
    ws = None
    start_time = time.time()

    try:
        # 1. 建立 WebSocket 连接（使用较短超时快速失败）
        connect_timeout = min(5, timeout // 3)
        logger.info(f"Polling session result: connecting to {ws_url}, session_key={session_key}")

        ws = websocket.create_connection(ws_url, timeout=connect_timeout, skip_utf8_validation=True)

        # 2. 尝试接收 connect.challenge（OpenClaw 会在连接后立即推送）
        nonce = _try_recv_challenge(ws)

        # 3. 构造 connect frame（带或不带设备认证）
        device_auth = None
        if nonce:
            device_auth = _build_device_auth(nonce)
            if device_auth:
                logger.info("Poll: using OpenClaw device auth for connect handshake")

        connect_frame = _build_connect_frame(gateway_token, device_auth=device_auth)
        ws.send(json.dumps(connect_frame))

        # 4. 等待握手响应（基于实际剩余时间动态分配）
        elapsed = time.time() - start_time
        handshake_timeout = max(5, min(15, timeout - elapsed - 5))  # 握手最多 15s，但不超过剩余时间
        ws.settimeout(handshake_timeout)

        # 等待握手响应（跳过 event 帧）
        response = None
        for _ in range(5):
            response_raw = ws.recv()
            response = json.loads(response_raw)
            if response.get("type") == "res":
                break
            logger.debug(
                f"Skipping non-res frame during poll handshake: type={response.get('type')}, event={response.get('event', '')}"
            )

        if not response or response.get("type") != "res":
            return {
                "status": "error",
                "error": f"Unexpected response type: {response.get('type') if response else 'none'}",
            }

        if not response.get("ok"):
            error = response.get("error", {})
            return {"status": "error", "error": f"Connect failed: {error.get('message', 'Unknown error')}"}

        payload = response.get("payload", {})
        if payload.get("type") != "hello-ok":
            return {"status": "error", "error": f"Unexpected payload type: {payload.get('type')}"}

        elapsed_hs = time.time() - start_time
        logger.info(f"Handshake OK in {elapsed_hs:.1f}s, sending chat.history request")

        # 4. 发送 chat.history 请求
        request_id = str(uuid.uuid4())
        history_request = {
            "type": "req",
            "id": request_id,
            "method": "chat.history",
            "params": {"sessionKey": session_key},
        }
        ws.send(json.dumps(history_request))

        # 5. 等待 chat.history 响应
        elapsed = time.time() - start_time
        remaining_timeout = max(15, timeout - elapsed - 3)  # chat.history 需要更多时间（高延迟网络）
        ws.settimeout(remaining_timeout)
        logger.info(f"chat.history sent, remaining_timeout={remaining_timeout:.1f}s")

        # 可能收到多个帧（event 帧等），需要找到匹配 id 的 res 帧
        max_frames = 50  # 避免无限循环
        frame_count = 0
        for _ in range(max_frames):
            frame_raw = ws.recv()
            if not frame_raw:
                continue

            frame_count += 1
            frame = json.loads(frame_raw)
            frame_type = frame.get("type", "")
            frame_id = frame.get("id", "")

            # 记录每个收到的帧（诊断用）
            if frame_type != "res" or frame_id != request_id:
                event_name = frame.get("event", "")
                logger.info(
                    f"chat.history wait: skip frame #{frame_count} type={frame_type}, event={event_name}, id={frame_id[:8] if frame_id else ''}"
                )

            # 只处理 type=="res" 且 id 匹配的响应
            if frame_type == "res" and frame_id == request_id:
                if not frame.get("ok"):
                    error = frame.get("error", {})
                    return {"status": "error", "error": f"chat.history failed: {error.get('message', 'Unknown error')}"}

                result_payload = frame.get("payload", {})
                messages = result_payload.get("messages", [])

                # 6. 解析结果：判断会话是否完成
                #
                # chat.history 返回的消息格式（每条 entry）：
                # {
                #     "id": "e673bdfc",
                #     "message": {
                #         "content": [{"text": "...", "type": "text"}],
                #         "durationMs": 81931,
                #         "role": "assistant",
                #         "timestamp": 1775206424794,
                #         "usage": {...}
                #     },
                #     "parentId": "471948bd",
                #     "timestamp": "2026-04-03T08:53:44Z",
                #     "type": "message"
                # }
                #
                # 判断逻辑：
                # - 如果没有消息，返回 pending
                # - 获取最后一条 entry，提取 message 字段
                # - 如果最后一条 role 不是 assistant，返回 pending
                # - 如果 content 包含 tool_use/toolCall/tool_call，返回 pending（还在调工具）
                # - 提取 text 内容，如果有实质内容则返回 completed

                if not messages:
                    logger.debug("Poll: no messages found, analysis pending")
                    return {"status": "pending"}

                # 获取最后一条 entry
                last_entry = messages[-1]

                # 兼容两种格式：嵌套格式 {message: {...}} 或扁平格式 {role, content, ...}
                msg = last_entry.get("message", last_entry)

                # 检查最后一条消息的角色
                role = msg.get("role", "")
                content = msg.get("content", [])
                has_duration = bool(msg.get("durationMs"))

                # 提取 content 类型列表用于日志
                content_types = []
                if isinstance(content, list):
                    content_types = [c.get("type") for c in content if isinstance(c, dict)]

                logger.info(
                    f"Poll: last message role={role}, content_types={content_types}, has_duration={has_duration}"
                )

                if role != "assistant":
                    # 最后一条是 user/toolResult 等，agent 还在处理
                    logger.debug(f"Poll: last message role is '{role}', analysis pending")
                    return {"status": "pending"}

                # 检查 content 是否包含工具调用（正在调用工具）
                has_tool_use = False
                text_parts = []

                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            item_type = item.get("type", "")
                            # 检测工具调用块：tool_use, toolUse, toolCall, tool_call
                            if item_type in ("tool_use", "toolUse", "toolCall", "tool_call"):
                                has_tool_use = True
                            # 提取 text 类型内容（排除 thinking）
                            elif item_type == "text":
                                text = item.get("text", "")
                                if text:
                                    text_parts.append(text)
                elif isinstance(content, str):
                    text_parts.append(content)

                # 如果包含工具调用，说明还在等待工具结果
                if has_tool_use:
                    logger.debug("Poll: message contains tool_use, analysis pending")
                    return {"status": "pending"}

                # 提取最终文本
                final_text = "\n".join(text_parts)

                # 如果没有文本内容，可能还在处理
                if not final_text:
                    logger.debug("Poll: no text content found, analysis pending")
                    return {"status": "pending"}

                # 会话完成
                logger.info(
                    f"Poll completed: found final assistant message, text length={len(final_text)}, msg_count={len(messages)}"
                )
                return {
                    "status": "completed",
                    "text": final_text,
                    "message": msg,
                    "msg_count": len(messages),  # 消息总数，用于稳定性检测
                }

        # 超过最大帧数仍未收到响应
        return {"status": "error", "error": "No response received for chat.history request"}

    except websocket.WebSocketTimeoutException:
        logger.warning(f"Poll timeout for session_key={session_key}")
        return {"status": "error", "error": f"Timeout ({timeout}s)"}
    except ConnectionRefusedError:
        return {"status": "error", "error": f"Connection refused: {ws_url}"}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"Invalid JSON response: {e}"}
    except websocket.WebSocketException as e:
        return {"status": "error", "error": f"WebSocket error: {e}"}
    except Exception as e:
        logger.error(f"Poll error: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        # 7. 确保关闭连接
        if ws:
            try:
                ws.close()
            except Exception as e:
                logger.debug(f"WebSocket close failed: {e}")


def wait_for_result(
    gateway_url: str, gateway_token: str, run_id: str, timeout: int = 300, connect_timeout: int | None = None
) -> dict:
    """
    连接 OpenClaw WebSocket，等待指定 runId 的分析结果。

    Args:
        gateway_url: OpenClaw Gateway HTTP URL (如 http://127.0.0.1:18900)
        gateway_token: Gateway 认证 token (OPENCLAW_GATEWAY_TOKEN)
        run_id: 要监听的分析任务 ID
        timeout: 等待分析结果超时时间（秒），默认 300
        connect_timeout: 连接超时时间（秒），默认 10，用于快速失败

    Returns:
        - 成功: {"status": "success", "run_id": "...", "message": {...}, "text": "完整文本"}
        - 失败: {"status": "error", "run_id": "...", "error": "错误信息", "error_type": "..."}
        - 超时: {"status": "timeout", "run_id": "...", "partial_text": "已收集的部分文本"}
    """
    client = OpenClawWSClient(gateway_url, gateway_token, run_id, timeout, connect_timeout)
    return client.wait_for_result()
