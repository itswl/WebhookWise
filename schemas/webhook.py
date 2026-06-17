from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.compression import decompress_payload
from core.datetime_utils import utc_isoformat

from .base import APIResponse, CursorPaginationInfo

DuplicateType = Literal["new", "within_window"]


class WebhookEventSummary(BaseModel):
    """Webhook event summary"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    request_id: str | None = None
    source: str
    client_ip: str | None = None
    ai_analysis: dict[str, Any] | None = Field(default=None, exclude=True)
    outbox_forward_status: str | None = Field(default=None, exclude=True)
    timestamp: str | None = None
    importance: str | None = None
    is_duplicate: bool
    duplicate_of: int | None = None
    duplicate_count: int
    duplicate_type: DuplicateType = "new"
    forward_status: str | None = None
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    acknowledged: bool = False
    summary: str | None = None
    created_at: str | None = None
    prev_alert_id: int | None = None
    prev_alert_timestamp: str | None = None
    is_within_window: bool = False

    @field_validator("timestamp", "created_at", "prev_alert_timestamp", "acknowledged_at", mode="before")
    @classmethod
    def _serialize_datetime(cls, value: object) -> object:
        return utc_isoformat(value) if isinstance(value, datetime) else value

    @model_validator(mode="after")
    def _derive_fields(self) -> WebhookEventSummary:
        if self.summary is None and isinstance(self.ai_analysis, dict):
            self.summary = str(self.ai_analysis.get("summary", "") or "")
        if self.is_duplicate:
            self.duplicate_type = "within_window"
            self.is_within_window = True
        if self.outbox_forward_status:
            self.forward_status = self.outbox_forward_status
        self.acknowledged = self.acknowledged_at is not None
        return self


class WebhookEventFull(WebhookEventSummary):
    """Webhook event details"""

    ai_analysis: dict[str, Any] | None = None
    raw_payload: str | None = None
    headers: dict[str, Any] | None = None
    parsed_data: dict[str, Any] | None = None
    alert_hash: str | None = None
    processing_status: str | None = None
    updated_at: str | None = None

    @field_validator("raw_payload", mode="before")
    @classmethod
    def _decompress_raw_payload(cls, value: object) -> object:
        if isinstance(value, bytearray):
            return decompress_payload(bytes(value))
        return decompress_payload(value) if isinstance(value, bytes) or value is None else value

    @field_validator("updated_at", mode="before")
    @classmethod
    def _serialize_updated_at(cls, value: object) -> object:
        return utc_isoformat(value) if isinstance(value, datetime) else value


class WebhookAckRequest(BaseModel):
    """Request body for acknowledging an alert."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    acknowledged_by: str = Field(default="", max_length=100)


class WebhookReceiveResponse(BaseModel):
    """Webhook receive response"""

    success: bool
    message: str
    event_id: int | None = None
    request_id: str


class WebhookListResponse(BaseModel):
    """Webhook list response"""

    success: bool
    data: list[WebhookEventSummary]
    status: int = 200
    pagination: CursorPaginationInfo


class HealthData(BaseModel):
    status: str
    database: str


class HealthResponse(APIResponse[HealthData]):
    pass


def webhook_event_to_summary_dict(event: Any) -> dict[str, Any]:
    return WebhookEventSummary.model_validate(event).model_dump(mode="json")


def webhook_event_to_full_dict(event: Any) -> dict[str, Any]:
    return WebhookEventFull.model_validate(event).model_dump(mode="json")
