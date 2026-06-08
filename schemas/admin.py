from __future__ import annotations

from pydantic import BaseModel, Field


class DeadLetterItem(BaseModel):
    """Dead letter 单条记录"""

    id: int
    source: str | None = None
    timestamp: str | None = None
    processing_status: str | None = None
    retry_count: int | None = None
    failure_reason: str | None = None
    error_message: str | None = None
    importance: str | None = None
    alert_hash: str | None = None
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
    event_ids: list[int] = Field(default_factory=list)
    skipped_event_ids: list[int] = Field(default_factory=list)


class ReplayBatchRequest(BaseModel):
    """指定 dead letter 批量重放请求"""

    event_ids: list[int] = Field(min_length=1, max_length=500)


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
