"""ASGI Bearer-token guard for the mounted MCP app.

The MCP Streamable-HTTP endpoint is a mounted Starlette sub-app, not a FastAPI
route, so it cannot use the ``verify_api_key`` ``Depends``. This middleware
performs the equivalent check at the ASGI layer: it accepts the same tokens as
the management API (``Authorization: Bearer`` / ``X-API-Key``) and compares them
against ``security.API_KEY`` in constant time.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from core.app_context import get_config_manager
from core.logger import get_logger

logger = get_logger("mcp")

_UNAUTHORIZED_BODY = b'{"error":"Invalid or missing API Key"}'


def _extract_tokens(headers: dict[bytes, bytes]) -> list[str]:
    """Pull candidate tokens from Authorization: Bearer and X-API-Key headers."""
    tokens: list[str] = []

    auth = headers.get(b"authorization")
    if auth:
        value = auth.decode("latin-1").strip()
        token = value[7:].strip() if value.lower().startswith("bearer ") else value
        if token:
            tokens.append(token)

    for key in (b"x-api-key", b"x-admin-key", b"x-admin-write-key"):
        raw = headers.get(key)
        if raw:
            token = raw.decode("latin-1").strip()
            if token:
                tokens.append(token)

    return tokens


class MCPAuthMiddleware:
    """Reject MCP requests that do not carry a valid management API key."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        api_key = get_config_manager().security.API_KEY
        headers = dict(scope.get("headers") or [])
        tokens = _extract_tokens(headers)

        authorized = bool(api_key) and any(hmac.compare_digest(t, api_key) for t in tokens)
        if not authorized:
            await self._reject(scope, send, reason="missing API_KEY config" if not api_key else "invalid token")
            return

        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, send: Send, *, reason: str) -> None:
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        logger.warning("[MCP] Unauthorized MCP access attempt ip=%s reason=%s", client_ip, reason)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})


AsgiHandler = Callable[[Scope, Receive, Send], Awaitable[None]]
