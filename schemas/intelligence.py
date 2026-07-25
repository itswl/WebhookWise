"""Contracts for change ingestion and incident-intelligence feedback."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.datetime_utils import naive_utc

RecommendationType = Literal["similar_incident", "change", "runbook"]
RecommendationVerdict = Literal["relevant", "irrelevant", "used", "not_used"]


class ChangeEventCreateRequest(BaseModel):
    """Normalized change event accepted from CI/CD and change systems."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_id: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=80)
    change_type: Literal["deployment", "config", "feature_flag", "infrastructure", "other"]
    project: str | None = Field(default=None, max_length=200)
    environment: str | None = Field(default=None, max_length=200)
    service: str | None = Field(default=None, max_length=200)
    region: str | None = Field(default=None, max_length=200)
    resource_type: str | None = Field(default=None, max_length=100)
    resource_id: str | None = Field(default=None, max_length=200)
    version_from: str | None = Field(default=None, max_length=200)
    version_to: str | None = Field(default=None, max_length=200)
    actor: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=30)
    started_at: datetime
    finished_at: datetime | None = None
    source_url: str | None = Field(default=None, max_length=500)
    details: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_interval(self) -> ChangeEventCreateRequest:
        self.started_at = naive_utc(self.started_at)
        if self.finished_at is not None:
            self.finished_at = naive_utc(self.finished_at)
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        return self


class IntelligenceFeedbackRequest(BaseModel):
    """Record whether an intelligence recommendation helped the operator."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recommendation_type: RecommendationType
    candidate_ref: str = Field(min_length=1, max_length=500)
    verdict: RecommendationVerdict
    comment: str | None = Field(default=None, max_length=4000)
    actor: str = Field(default="operator", min_length=1, max_length=100)
