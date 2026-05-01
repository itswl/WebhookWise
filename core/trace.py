"""全链路 Trace ID 管理。"""

from __future__ import annotations

import contextvars
import hashlib
import secrets
import uuid

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def _normalize_trace_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if len(lowered) == 32 and all(c in "0123456789abcdef" for c in lowered):
        return lowered
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def generate_trace_id(event_id: int | None = None) -> str:
    if event_id:
        return _normalize_trace_id(f"evt-{event_id}")
    return uuid.uuid4().hex


def set_trace_id(tid: str) -> contextvars.Token:
    return trace_id_var.set(_normalize_trace_id(tid))


def get_trace_id() -> str:
    return trace_id_var.get()


def build_traceparent(trace_id: str) -> str:
    tid_hex = _normalize_trace_id(trace_id)
    span_id = secrets.token_hex(8)
    return f"00-{tid_hex}-{span_id}-01"


def extract_trace_id_from_headers(headers: dict) -> str:
    xrid = (headers.get("x-request-id") or headers.get("X-Request-Id") or "").strip()
    if xrid:
        return _normalize_trace_id(xrid)
    tp = (headers.get("traceparent") or headers.get("Traceparent") or "").strip()
    if not tp:
        return ""
    parts = tp.split("-")
    if len(parts) != 4:
        return ""
    trace_id = parts[1]
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id.lower()):
        return ""
    return trace_id.lower()
