"""深度分析相关响应模型"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class DeepAnalysisRecord(BaseModel):
    """深度分析记录 —— 对应 DeepAnalysis.to_dict()"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    webhook_event_id: int
    engine: str | None = None
    user_question: str | None = None
    analysis_result: dict | str | None = None
    duration_seconds: float | None = None
    created_at: str | None = None
    openclaw_run_id: str | None = None
    openclaw_session_key: str | None = None
    status: str | None = None


class DeepAnalysisListData(BaseModel):
    """深度分析列表数据（含分页）"""

    total: int | None = None
    total_pages: int | None = None
    page: int | None = None
    per_page: int
    next_cursor: int | None = None
    items: list[DeepAnalysisRecord]


class DeepAnalysisListResponse(BaseModel):
    """深度分析列表响应"""

    success: bool
    data: DeepAnalysisListData


class ReanalysisResponse(BaseModel):
    """重新分析响应"""

    success: bool
    status: str | None = None
    analysis: dict[str, Any] | None = None
    original_importance: str | None = None
    new_importance: str | None = None
    updated_duplicates: int | None = None
    message: str | None = None
