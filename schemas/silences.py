from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.datetime_utils import utc_isoformat

from .base import APIResponse


class SilenceCreateData(TypedDict):
    match_source: str
    match_importance: str
    match_event_type: str
    match_project: str
    match_region: str
    match_environment: str
    match_payload: str
    comment: str
    created_by: str
    expires_at: datetime | None


class SilenceUpdateData(TypedDict, total=False):
    match_source: str
    match_importance: str
    match_event_type: str
    match_project: str
    match_region: str
    match_environment: str
    match_payload: str
    comment: str
    expires_at: datetime | None


class _SilenceRequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    match_source: str = Field(default="", max_length=200)
    match_importance: str = Field(default="", max_length=50)
    match_event_type: str = Field(default="", max_length=200)
    match_project: str = Field(default="", max_length=200)
    match_region: str = Field(default="", max_length=200)
    match_environment: str = Field(default="", max_length=200)
    match_payload: str = Field(default="", max_length=512)
    comment: str = Field(default="", max_length=500)
    created_by: str = Field(default="", max_length=100)


class SilenceCreateRequest(_SilenceRequestBase):
    """Request body for creating a silence.

    expires_at is optional: omit (or null) to keep the silence active until it is
    manually lifted. A silence with no match criteria matches everything, which
    is rarely intended, so at least one criterion must be set.
    """

    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _require_a_criterion(self) -> SilenceCreateRequest:
        criteria = (
            self.match_source,
            self.match_importance,
            self.match_event_type,
            self.match_project,
            self.match_region,
            self.match_environment,
            self.match_payload,
        )
        if not any(c.strip() for c in criteria):
            raise ValueError("At least one match criterion must be provided")
        return self

    def to_service_kwargs(self) -> SilenceCreateData:
        return {
            "match_source": self.match_source,
            "match_importance": self.match_importance,
            "match_event_type": self.match_event_type,
            "match_project": self.match_project,
            "match_region": self.match_region,
            "match_environment": self.match_environment,
            "match_payload": self.match_payload,
            "comment": self.comment,
            "created_by": self.created_by,
            "expires_at": self.expires_at,
        }


class SilenceUpdateRequest(BaseModel):
    """Request body for updating a silence.

    Fields are optional for PATCH-like semantics. expires_at may be set to null
    to make a silence permanent; the other fields reject explicit nulls because
    they store concrete primitive values.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    match_source: str | None = Field(default=None, max_length=200)
    match_importance: str | None = Field(default=None, max_length=50)
    match_event_type: str | None = Field(default=None, max_length=200)
    match_project: str | None = Field(default=None, max_length=200)
    match_region: str | None = Field(default=None, max_length=200)
    match_environment: str | None = Field(default=None, max_length=200)
    match_payload: str | None = Field(default=None, max_length=512)
    comment: str | None = Field(default=None, max_length=500)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _reject_empty_or_invalid_null(self) -> SilenceUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("At least one field to update must be provided")
        # expires_at=null is meaningful (make permanent); the rest may not be null.
        null_fields = [
            field for field in self.model_fields_set if field != "expires_at" and getattr(self, field) is None
        ]
        if null_fields:
            raise ValueError(f"Fields are not allowed to be null: {', '.join(sorted(null_fields))}")
        return self

    def to_update_payload(self) -> SilenceUpdateData:
        return cast(SilenceUpdateData, self.model_dump(exclude_unset=True))


class SilenceSchema(BaseModel):
    """A silence row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    match_source: str | None = None
    match_importance: str | None = None
    match_event_type: str | None = None
    match_project: str | None = None
    match_region: str | None = None
    match_environment: str | None = None
    match_payload: str | None = None
    comment: str | None = None
    created_by: str | None = None
    created_at: datetime | str | None = None
    expires_at: datetime | str | None = None
    lifted_at: datetime | str | None = None
    active: bool = True
    # ROI signals: how many alerts this silence has suppressed (lifetime, from the
    # decision trace) and when it last did. Annotated by the list endpoint; absent
    # on create/update responses (no trace lookup there), hence the defaults.
    suppressed_count: int = 0
    last_suppressed_at: datetime | str | None = None


class SilenceListResponse(APIResponse[list[SilenceSchema]]):
    """Silence list response."""


class SilenceDetailResponse(APIResponse[SilenceSchema]):
    """Silence detail response."""


def _is_active(silence: Any, now: datetime) -> bool:
    if getattr(silence, "lifted_at", None) is not None:
        return False
    expires_at = getattr(silence, "expires_at", None)
    return not (expires_at is not None and expires_at <= now)


def silence_to_dict(silence: Any, *, now: datetime) -> dict[str, Any]:
    data = SilenceSchema.model_validate(silence).model_dump()
    data["active"] = _is_active(silence, now)
    for field in ("created_at", "expires_at", "lifted_at"):
        if isinstance(data.get(field), datetime):
            data[field] = utc_isoformat(data[field])
    return data
