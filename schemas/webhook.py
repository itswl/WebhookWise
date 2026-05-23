from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from core.compression import decompress_payload

from .base import APIResponse, CursorPaginationInfo

DuplicateType = Literal["new", "within_window", "beyond_window"]


class WebhookEventSummary(BaseModel):
    """Webhook 事件摘要"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    request_id: str | None = None
    source: str
    client_ip: str | None = None
    timestamp: str | None = None
    importance: str | None = None
    is_duplicate: bool
    duplicate_of: int | None = None
    duplicate_count: int
    beyond_window: bool
    duplicate_type: DuplicateType = "new"
    forward_status: str | None = None
    summary: str | None = None
    alert_info: dict[str, Any] | None = None
    created_at: str | None = None
    prev_alert_id: int | None = None


class WebhookReceiveResponse(BaseModel):
    """Webhook 接收响应"""

    success: bool
    message: str
    event_id: int | None = None
    request_id: str


class WebhookListResponse(BaseModel):
    """Webhook 列表响应"""

    success: bool
    data: list[WebhookEventSummary]
    status: int = 200
    pagination: CursorPaginationInfo


class HealthData(BaseModel):
    status: str
    database: str


class HealthResponse(APIResponse[HealthData]):
    pass


def _iso_or_none(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def mongodb_summary_fields(parsed_data: Any | None) -> dict[str, Any]:
    if not parsed_data or not isinstance(parsed_data, dict):
        return {}
    monitor = parsed_data.get("监控项")
    return {
        "host": monitor.get("主机", "") if isinstance(monitor, dict) else "",
        "metric": monitor.get("监控项", "") if isinstance(monitor, dict) else "",
        "value": parsed_data.get("当前值", ""),
    }


def webhook_event_to_summary_dict(event: Any) -> dict[str, Any]:
    ai_analysis = getattr(event, "ai_analysis", None)
    parsed_data = getattr(event, "parsed_data", None)
    source = str(getattr(event, "source", ""))
    is_duplicate = bool(getattr(event, "is_duplicate", False))
    beyond_window = bool(getattr(event, "beyond_window", False))
    duplicate_type: DuplicateType = "new"
    if is_duplicate:
        duplicate_type = "beyond_window" if beyond_window else "within_window"
    return {
        "id": event.id,
        "request_id": getattr(event, "request_id", None),
        "source": source,
        "client_ip": getattr(event, "client_ip", None),
        "timestamp": _iso_or_none(getattr(event, "timestamp", None)),
        "importance": getattr(event, "importance", None),
        "is_duplicate": is_duplicate,
        "duplicate_of": getattr(event, "duplicate_of", None),
        "duplicate_count": int(getattr(event, "duplicate_count", 0) or 0),
        "beyond_window": beyond_window,
        "duplicate_type": duplicate_type,
        "forward_status": getattr(event, "forward_status", None),
        "summary": ai_analysis.get("summary", "") if isinstance(ai_analysis, dict) else None,
        "alert_info": mongodb_summary_fields(parsed_data) if source == "mongodb" else {},
        "created_at": _iso_or_none(getattr(event, "created_at", None)),
        "prev_alert_id": getattr(event, "prev_alert_id", None),
    }


def webhook_event_to_full_dict(event: Any) -> dict[str, Any]:
    return {
        **webhook_event_to_summary_dict(event),
        "raw_payload": decompress_payload(getattr(event, "raw_payload", None)),
        "headers": getattr(event, "headers", None),
        "parsed_data": getattr(event, "parsed_data", None),
        "alert_hash": getattr(event, "alert_hash", None),
        "ai_analysis": getattr(event, "ai_analysis", None),
        "processing_status": getattr(event, "processing_status", None),
        "updated_at": _iso_or_none(getattr(event, "updated_at", None)),
    }
