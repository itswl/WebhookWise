"""全链路 Trace ID 管理。"""

from __future__ import annotations

import contextvars
import hashlib
import secrets
import uuid

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def generate_trace_id(event_id: int | None = None) -> str:
    """生成 trace_id。优先使用 event_id，否则生成 UUID 短码。"""
    if event_id:
        return f"evt-{event_id}"
    return uuid.uuid4().hex[:12]


def set_trace_id(tid: str) -> contextvars.Token:
    """设置当前协程的 trace_id。"""
    return trace_id_var.set(tid)


def get_trace_id() -> str:
    """获取当前协程的 trace_id。"""
    return trace_id_var.get()


def build_traceparent(trace_id: str) -> str:
    raw = (trace_id or "").strip()
    if len(raw) == 32 and all(c in "0123456789abcdef" for c in raw):
        tid_hex = raw
    else:
        tid_hex = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    span_id = secrets.token_hex(8)
    return f"00-{tid_hex}-{span_id}-01"


def extract_trace_id_from_headers(headers: dict) -> str:
    xrid = (headers.get("x-request-id") or headers.get("X-Request-Id") or "").strip()
    if xrid:
        return xrid
    tp = (headers.get("traceparent") or headers.get("Traceparent") or "").strip()
    if not tp:
        return ""
    parts = tp.split("-")
    if len(parts) != 4:
        return ""
    trace_id = parts[1]
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id.lower()):
        return ""
    return trace_id[:12]
