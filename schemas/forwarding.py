from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.datetime_utils import utc_isoformat

from .base import APIResponse

ForwardTargetType = Literal["feishu", "openclaw", "webhook"]
ForwardDuplicateMode = Literal["all", "new", "duplicate"]
FORWARD_RULE_PRIORITY_MIN = -1_000_000
FORWARD_RULE_PRIORITY_MAX = 1_000_000


class ForwardRuleCreateData(TypedDict):
    name: str
    target_type: ForwardTargetType
    enabled: bool
    priority: int
    match_event_type: str
    match_importance: str
    match_duplicate: ForwardDuplicateMode
    match_source: str
    match_project: str
    match_region: str
    match_environment: str
    match_payload: str
    target_url: str
    target_name: str
    stop_on_match: bool


class ForwardRuleUpdateData(TypedDict, total=False):
    name: str
    enabled: bool
    priority: int
    match_event_type: str
    match_importance: str
    match_duplicate: ForwardDuplicateMode
    match_source: str
    match_project: str
    match_region: str
    match_environment: str
    match_payload: str
    target_type: ForwardTargetType
    target_url: str
    target_name: str
    stop_on_match: bool


class _ForwardRuleRequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = True
    priority: int = Field(default=0, ge=FORWARD_RULE_PRIORITY_MIN, le=FORWARD_RULE_PRIORITY_MAX)
    match_event_type: str = Field(default="", max_length=200)
    match_importance: str = Field(default="", max_length=50)
    match_duplicate: ForwardDuplicateMode = "all"
    match_source: str = Field(default="", max_length=200)
    match_project: str = Field(default="", max_length=200)
    match_region: str = Field(default="", max_length=200)
    match_environment: str = Field(default="", max_length=200)
    match_payload: str = Field(default="", max_length=512)
    target_url: str = Field(default="", max_length=500)
    target_name: str = Field(default="", max_length=100)
    stop_on_match: bool = False


class ForwardRuleCreateRequest(_ForwardRuleRequestBase):
    """Request body for creating a forwarding rule."""

    name: str = Field(min_length=1, max_length=100)
    target_type: ForwardTargetType

    def to_service_kwargs(self) -> ForwardRuleCreateData:
        return {
            "name": self.name,
            "target_type": self.target_type,
            "enabled": self.enabled,
            "priority": self.priority,
            "match_event_type": self.match_event_type,
            "match_importance": self.match_importance,
            "match_duplicate": self.match_duplicate,
            "match_source": self.match_source,
            "match_project": self.match_project,
            "match_region": self.match_region,
            "match_environment": self.match_environment,
            "match_payload": self.match_payload,
            "target_url": self.target_url,
            "target_name": self.target_name,
            "stop_on_match": self.stop_on_match,
        }


class ForwardRuleUpdateRequest(BaseModel):
    """Request body for updating a forwarding rule.

    Fields are optional for PATCH-like semantics, but explicit nulls are not
    accepted because the database contract stores concrete primitive values.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=FORWARD_RULE_PRIORITY_MIN, le=FORWARD_RULE_PRIORITY_MAX)
    match_event_type: str | None = Field(default=None, max_length=200)
    match_importance: str | None = Field(default=None, max_length=50)
    match_duplicate: ForwardDuplicateMode | None = None
    match_source: str | None = Field(default=None, max_length=200)
    match_project: str | None = Field(default=None, max_length=200)
    match_region: str | None = Field(default=None, max_length=200)
    match_environment: str | None = Field(default=None, max_length=200)
    match_payload: str | None = Field(default=None, max_length=512)
    target_type: ForwardTargetType | None = None
    target_url: str | None = Field(default=None, max_length=500)
    target_name: str | None = Field(default=None, max_length=100)
    stop_on_match: bool | None = None

    @model_validator(mode="after")
    def _reject_empty_or_null_update(self) -> ForwardRuleUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("至少提供一个更新字段")
        null_fields = [field for field in self.model_fields_set if getattr(self, field) is None]
        if null_fields:
            raise ValueError(f"字段不允许为 null: {', '.join(sorted(null_fields))}")
        return self

    def to_update_payload(self) -> ForwardRuleUpdateData:
        return cast(ForwardRuleUpdateData, self.model_dump(exclude_unset=True))


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
    match_project: str | None = None
    match_region: str | None = None
    match_environment: str | None = None
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
    data = ForwardRuleSchema.model_validate(rule).model_dump()
    for field in ("created_at", "updated_at"):
        if isinstance(data.get(field), datetime):
            data[field] = utc_isoformat(data[field])
    return data
