from __future__ import annotations

import contextvars

event_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("event_id", default=None)
alert_hash_var: contextvars.ContextVar[str] = contextvars.ContextVar("alert_hash", default="")
source_var: contextvars.ContextVar[str] = contextvars.ContextVar("source", default="")
processing_status_var: contextvars.ContextVar[str] = contextvars.ContextVar("processing_status", default="")
route_type_var: contextvars.ContextVar[str] = contextvars.ContextVar("route_type", default="")


def set_log_context(
    *,
    event_id: int | None = None,
    alert_hash: str | None = None,
    source: str | None = None,
    processing_status: str | None = None,
    route_type: str | None = None,
) -> None:
    if event_id is not None:
        event_id_var.set(event_id)
    if alert_hash is not None:
        alert_hash_var.set(alert_hash)
    if source is not None:
        source_var.set(source)
    if processing_status is not None:
        processing_status_var.set(processing_status)
    if route_type is not None:
        route_type_var.set(route_type)


def get_log_context() -> dict:
    return {
        "event_id": event_id_var.get(),
        "alert_hash": alert_hash_var.get(),
        "source": source_var.get(),
        "processing_status": processing_status_var.get(),
        "route_type": route_type_var.get(),
    }


def clear_log_context() -> None:
    event_id_var.set(None)
    alert_hash_var.set("")
    source_var.set("")
    processing_status_var.set("")
    route_type_var.set("")

