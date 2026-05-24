from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from .base import APIResponse


class ForwardRuleSchema(BaseModel):
    """转发规则"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    priority: int
    match_event_type: str | None = None
    match_importance: str | None = None
    match_duplicate: str | None = None
    match_source: str | None = None
    match_payload: str | None = None
    target_type: str
    target_url: str
    target_name: str | None = None
    stop_on_match: bool
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None


class ForwardRuleListResponse(APIResponse[list[ForwardRuleSchema]]):
    """转发规则列表响应"""


class ForwardRuleDetailResponse(APIResponse[ForwardRuleSchema]):
    """转发规则详情响应"""


def forward_rule_to_dict(rule: Any) -> dict[str, Any]:
    return ForwardRuleSchema.model_validate(rule).model_dump(mode="json")
