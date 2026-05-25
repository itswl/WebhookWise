from __future__ import annotations

import contextvars

from core.observability.attributes import (
    REQUEST_ID,
    WEBHOOK_ALERT_HASH,
    WEBHOOK_EVENT_ID,
    WEBHOOK_ROUTE,
    WEBHOOK_SOURCE,
    WEBHOOK_STATUS,
)

webhook_event_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(WEBHOOK_EVENT_ID, default=None)
webhook_alert_hash_var: contextvars.ContextVar[str] = contextvars.ContextVar(WEBHOOK_ALERT_HASH, default="")
webhook_source_var: contextvars.ContextVar[str] = contextvars.ContextVar(WEBHOOK_SOURCE, default="")
webhook_status_var: contextvars.ContextVar[str] = contextvars.ContextVar(WEBHOOK_STATUS, default="")
webhook_route_var: contextvars.ContextVar[str] = contextvars.ContextVar(WEBHOOK_ROUTE, default="")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(REQUEST_ID, default="")


def set_log_context(
    *,
    event_id: int | None = None,
    request_id: str | None = None,
    alert_hash: str | None = None,
    webhook_source: str | None = None,
    webhook_status: str | None = None,
    webhook_route: str | None = None,
) -> None:
    if event_id is not None:
        webhook_event_id_var.set(event_id)
    if request_id is not None:
        request_id_var.set(request_id)
    if alert_hash is not None:
        webhook_alert_hash_var.set(alert_hash)
    if webhook_source is not None:
        webhook_source_var.set(webhook_source)
    if webhook_status is not None:
        webhook_status_var.set(webhook_status)
    if webhook_route is not None:
        webhook_route_var.set(webhook_route)


def get_log_context() -> dict[str, object]:
    ctx: dict[str, object] = {}
    event_id = webhook_event_id_var.get()
    if event_id is not None:
        ctx[WEBHOOK_EVENT_ID] = event_id

    request_id = request_id_var.get()
    if request_id:
        ctx[REQUEST_ID] = request_id

    alert_hash = webhook_alert_hash_var.get()
    if alert_hash:
        ctx[WEBHOOK_ALERT_HASH] = alert_hash

    source = webhook_source_var.get()
    if source:
        ctx[WEBHOOK_SOURCE] = source

    processing_status = webhook_status_var.get()
    if processing_status:
        ctx[WEBHOOK_STATUS] = processing_status

    route_type = webhook_route_var.get()
    if route_type:
        ctx[WEBHOOK_ROUTE] = route_type
    return ctx


def clear_log_context() -> None:
    webhook_event_id_var.set(None)
    request_id_var.set("")
    webhook_alert_hash_var.set("")
    webhook_source_var.set("")
    webhook_status_var.set("")
    webhook_route_var.set("")
