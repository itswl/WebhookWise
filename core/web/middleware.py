import time
from collections.abc import Callable, MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from core.log_context import clear_log_context, set_log_context
from core.logger import logger
from core.metrics import (
    HTTP_SERVER_REQUEST_BODY_BYTES,
    HTTP_SERVER_REQUEST_DURATION_SECONDS,
    HTTP_SERVER_REQUESTS_TOTAL,
)
from core.trace import build_traceparent, extract_trace_id_from_headers, generate_trace_id, set_trace_id, trace_id_var


def _route_label(path: str) -> str:
    if path in {"/", "/dashboard", "/live", "/ready", "/health", "/webhook", "/api/webhooks"}:
        return path
    if path.startswith("/static/"):
        return "/static/*"
    if path.startswith("/webhook/"):
        return "/webhook/{source}"
    if path.startswith("/api/admin/"):
        return "/api/admin/*"
    if path.startswith("/api/forwarding/"):
        return "/api/forwarding/*"
    if path.startswith("/api/deep-analysis/"):
        return "/api/deep-analysis/*"
    if path.startswith("/api/reanalysis/"):
        return "/api/reanalysis/*"
    if path.startswith("/api/ai-usage/"):
        return "/api/ai-usage/*"
    return "other"


def _content_length_bytes(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class SecurityHeadersMiddleware:
    """Pure ASGI middleware, avoiding BaseHTTPMiddleware task isolation."""

    _EXTRA_HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
    ]

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing_names = {h[0].lower() for h in headers}
                for name, value in self._EXTRA_HEADERS:
                    if name.lower() not in existing_names:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestBodyLimitExceeded(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes_provider: Callable[[], int]) -> None:
        self.app = app
        self.max_body_bytes_provider = max_body_bytes_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = max(0, int(self.max_body_bytes_provider() or 0))
        if max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers") or []}
        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    await self._send_413(send, max_bytes)
                    return
            except ValueError:
                pass

        seen = 0

        async def limited_receive() -> MutableMapping[str, Any]:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                seen += len(body) if isinstance(body, bytes) else 0
                if seen > max_bytes:
                    raise RequestBodyLimitExceeded
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyLimitExceeded:
            await self._send_413(send, max_bytes)

    async def _send_413(self, send: Send, max_bytes: int) -> None:
        body = f'{{"success":false,"error":"Payload too large","max_bytes":{max_bytes}}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class TraceContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers") or []}
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        client = scope.get("client")
        client_ip = client[0] if isinstance(client, tuple) and client else ""
        content_length = headers.get("content-length", "")
        incoming = extract_trace_id_from_headers(headers)
        if incoming and "traceparent" not in headers:
            raw_headers = list(scope.get("headers") or [])
            raw_headers.append((b"traceparent", build_traceparent(incoming).encode("latin1")))
            scope["headers"] = raw_headers

        # Prefer the active OTEL span trace id so logs and traces can be joined.
        from core.otel import get_otel_trace_id

        otel_tid = get_otel_trace_id()
        token = set_trace_id(otel_tid or incoming or generate_trace_id())
        clear_log_context()
        set_log_context(request_id=trace_id_var.get())
        started_at = time.perf_counter()
        status_code = 500

        async def send_with_status(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 0)
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
            otel_tid = get_otel_trace_id()
            if otel_tid:
                set_trace_id(otel_tid)
        except Exception:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            logger.exception(
                "[HTTP] 请求异常 method=%s path=%s status=%s duration=%dms ip=%s content_length=%s",
                method,
                path,
                status_code,
                duration_ms,
                client_ip,
                content_length,
            )
            raise
        finally:
            route = _route_label(path)
            status = str(status_code or 500)
            duration_seconds = time.perf_counter() - started_at
            HTTP_SERVER_REQUESTS_TOTAL.labels(method, route, status).inc()
            HTTP_SERVER_REQUEST_DURATION_SECONDS.labels(method, route, status).observe(duration_seconds)
            if (body_bytes := _content_length_bytes(content_length)) is not None:
                HTTP_SERVER_REQUEST_BODY_BYTES.labels(method, route).observe(body_bytes)

            if path not in {"/live", "/ready", "/health"} and not path.startswith("/static/"):
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                logger.info(
                    "[HTTP] 请求完成 method=%s path=%s status=%s duration=%dms ip=%s content_length=%s",
                    method,
                    path,
                    status_code,
                    duration_ms,
                    client_ip,
                    content_length,
                )
            clear_log_context()
            trace_id_var.reset(token)
