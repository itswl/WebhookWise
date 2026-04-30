"""转发规则相关响应模型"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from schemas.base import APIResponse


class ForwardRuleSchema(BaseModel):
    """转发规则 —— 对应 ForwardRule.to_dict()"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    priority: int
    match_importance: str | None = None
    match_duplicate: str | None = None
    match_source: str | None = None
    target_type: str
    target_url: str
    target_name: str | None = None
    stop_on_match: bool
    created_at: str | None = None
    updated_at: str | None = None


class ForwardRuleListResponse(APIResponse[list[ForwardRuleSchema]]):
    """转发规则列表响应"""


class ForwardRuleDetailResponse(APIResponse[ForwardRuleSchema]):
    """转发规则详情响应"""
