from __future__ import annotations

from pydantic import BaseModel, Field


class DeadLetterItem(BaseModel):
    """Single dead letter record"""

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
    """Dead letter list response"""

    success: bool
    data: list[DeadLetterItem]
    pagination: DeadLetterPagination


class ReplayResponse(BaseModel):
    """Single dead letter replay response"""

    success: bool
    message: str
    event_id: int


class ReplayAllResponse(BaseModel):
    """Batch replay response"""

    success: bool
    message: str
    replayed: int
    event_ids: list[int] = Field(default_factory=list)
    skipped_event_ids: list[int] = Field(default_factory=list)


class ReplayBatchRequest(BaseModel):
    """Request to batch replay specified dead letters"""

    event_ids: list[int] = Field(min_length=1, max_length=500)


class KBDocumentRequest(BaseModel):
    """Request to ingest one knowledge-base document (chunked + embedded server-side)."""

    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    source_ref: str | None = Field(default=None, max_length=500)
    tags: dict[str, str] | None = None


class ConfigImportRequest(BaseModel):
    """Request to import a previously exported YAML config bundle."""

    # 2 MB YAML ceiling — a real bundle is a few KB; this bounds parse work.
    content: str = Field(min_length=1, max_length=2_000_000)
    dry_run: bool = False


class KBDocumentResponse(BaseModel):
    """Knowledge-base ingest response."""

    success: bool
    title: str
    chunks: int
    embedding_model: str


class PromptGetResponse(BaseModel):
    """Get prompt template response"""

    success: bool
    kind: str = "user"
    template: str
    source: str


class PromptReloadResponse(BaseModel):
    """Reload prompt template response"""

    success: bool
    message: str
    kind: str = "user"
    source: str | None = None
    template_length: int
    preview: str
