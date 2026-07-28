"""Contracts for managed inbound-source onboarding."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SOURCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"


class SourceConnectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    source_type: str = Field(min_length=1, max_length=100, pattern=_SOURCE_PATTERN)
    actor: str = Field(default="operator", min_length=1, max_length=100)


class SourceConnectionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    source_type: str | None = Field(default=None, min_length=1, max_length=100, pattern=_SOURCE_PATTERN)
    enabled: bool | None = None
    actor: str = Field(default="operator", min_length=1, max_length=100)

    @model_validator(mode="after")
    def _require_change(self) -> SourceConnectionUpdateRequest:
        if not (self.model_fields_set - {"actor"}):
            raise ValueError("At least one source connection field must be provided")
        return self


class SourceConnectionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str = Field(default="operator", min_length=1, max_length=100)
