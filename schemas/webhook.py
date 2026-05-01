"""Webhook 事件相关响应模型"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from schemas.base import APIResponse, CursorPaginationInfo

DuplicateType = Literal["new", "within_window", "beyond_window"]


class WebhookEventFull(BaseModel):
    """完整 Webhook 事件 —— 对应 WebhookEvent.to_dict()"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    client_ip: str | None = None
    timestamp: str | None = None
    raw_payload: str | None = None
    headers: dict | None = None
    parsed_data: dict | None = None
    alert_hash: str | None = None
    ai_analysis: dict | None = None
    importance: str | None = None
    processing_status: str
    forward_status: str | None = None
    is_duplicate: bool
    duplicate_of: int | None = None
    duplicate_count: int
    beyond_window: bool
    beyond_time_window: bool = False
    is_within_window: bool = False
    duplicate_type: DuplicateType = "new"
    created_at: str | None = None
    updated_at: str | None = None
    prev_alert_id: int | None = None
    prev_alert_timestamp: str | None = None


class WebhookEventSummary(BaseModel):
    """Webhook 事件摘要 —— 对应 WebhookEvent.to_summary_dict()"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    client_ip: str | None = None
    timestamp: str | None = None
    importance: str | None = None
    is_duplicate: bool
    duplicate_of: int | None = None
    duplicate_count: int
    beyond_window: bool
    beyond_time_window: bool = False
    is_within_window: bool = False
    duplicate_type: DuplicateType = "new"
    forward_status: str | None = None
    summary: str | None = None
    alert_info: dict | None = None
    created_at: str | None = None
    prev_alert_id: int | None = None
    prev_alert_timestamp: str | None = None


class WebhookReceiveResponse(BaseModel):
    """Webhook 接收响应"""

    success: bool
    message: str
    event_id: int


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


class WebhookDetailResponse(APIResponse[WebhookEventFull]):
    pass
