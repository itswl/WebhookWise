from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .base import APIResponse


class DeadLetterItem(BaseModel):
    """Dead letter 单条记录"""

    id: int
    source: str | None = None
    timestamp: str | None = None
    processing_status: str | None = None
    retry_count: int | None = None
    created_at: str | None = None


class DeadLetterPagination(BaseModel):
    page: int
    page_size: int
    total: int | None = None


class DeadLetterListResponse(BaseModel):
    """Dead letter 列表响应"""

    success: bool
    data: list[DeadLetterItem]
    pagination: DeadLetterPagination


class ReplayResponse(BaseModel):
    """单条 dead letter 重放响应"""

    success: bool
    message: str
    event_id: int


class ReplayAllResponse(BaseModel):
    """批量重放响应"""

    success: bool
    message: str
    replayed: int
    event_ids: list[int] = []


class StuckEventItem(BaseModel):
    id: int
    source: str | None = None
    processing_status: str | None = None
    retry_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class StuckEventListResponse(BaseModel):
    success: bool
    data: list[StuckEventItem]


class StuckEventRequeueResponse(BaseModel):
    success: bool
    message: str
    event_id: int


class ConfigResponse(APIResponse[dict[str, Any]]):
    """配置读取响应"""


class ConfigUpdateResponse(BaseModel):
    """配置更新响应"""

    success: bool
    message: str


class ConfigSourceItem(BaseModel):
    key: str
    source: str
    updated_at: str | None = None
    updated_by: str | None = None
    requires_restart: bool = False


class ConfigSourcesResponse(APIResponse[list[ConfigSourceItem]]):
    """配置来源响应"""


class PromptGetResponse(BaseModel):
    """获取 Prompt 模板响应"""

    success: bool
    kind: str = "user"
    template: str
    source: str


class PromptReloadResponse(BaseModel):
    """重载 Prompt 模板响应"""

    success: bool
    message: str
    kind: str = "user"
    source: str | None = None
    template_length: int
    preview: str
