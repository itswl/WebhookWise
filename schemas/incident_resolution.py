"""Contracts for human-confirmed incident resolution and recurrence review."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ChangeAssociation = Literal["confirmed", "suspected", "ruled_out", "unknown"]
FollowUpItem = Annotated[str, Field(min_length=1, max_length=500)]


class IncidentResolutionRequest(BaseModel):
    """Partially update an incident's operator-owned resolution record."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    root_cause_category: str | None = Field(default=None, max_length=80)
    root_cause: str | None = Field(default=None, max_length=4000)
    resolution: str | None = Field(default=None, max_length=4000)
    impact: str | None = Field(default=None, max_length=4000)
    change_association: ChangeAssociation | None = None
    related_change_id: int | None = Field(default=None, ge=1)
    recovery_evidence: str | None = Field(default=None, max_length=4000)
    owner: str | None = Field(default=None, max_length=100)
    follow_ups: list[FollowUpItem] | None = Field(default=None, max_length=30)
    actor: str = Field(default="operator", min_length=1, max_length=100)

    @model_validator(mode="after")
    def _validate_draft(self) -> IncidentResolutionRequest:
        if self.follow_ups is not None:
            cleaned = [" ".join(item.split()).strip() for item in self.follow_ups]
            if any(not item for item in cleaned):
                raise ValueError("follow_ups cannot contain empty items")
            if len(cleaned) != len(set(cleaned)):
                raise ValueError("follow_ups must be unique")
            self.follow_ups = cleaned
        return self


class IncidentRecurrenceReviewRequest(BaseModel):
    """Confirm or dismiss one pending recurrence association."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str = Field(default="operator", min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
