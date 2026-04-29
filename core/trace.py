"""全链路 Trace ID 管理。"""

from __future__ import annotations

import contextvars
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
