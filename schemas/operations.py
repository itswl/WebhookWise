"""Contracts for operator workflow, feedback, and incident editing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

WorkflowStatus = Literal["open", "acknowledged", "in_progress", "resolved", "ignored"]
FeedbackVerdict = Literal[
    "correct",
    "incorrect",
    "noise",
    "severity_too_high",
    "severity_too_low",
    "grouping_wrong",
    "should_group",
]


class WorkflowUpdateRequest(BaseModel):
    """Patch an alert or incident's operational workflow state."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    workflow_status: WorkflowStatus | None = None
    assignee: str | None = Field(default=None, max_length=100)
    team: str | None = Field(default=None, max_length=100)
    sla_minutes: int | None = Field(default=None, ge=1, le=525_600)
    clear_sla: bool = False

    @model_validator(mode="after")
    def _validate_patch(self) -> WorkflowUpdateRequest:
        mutable = self.model_fields_set - {"clear_sla"}
        if not mutable and not self.clear_sla:
            raise ValueError("At least one workflow field must be provided")
        if self.sla_minutes is not None and self.clear_sla:
            raise ValueError("sla_minutes and clear_sla cannot be used together")
        return self


class NoteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    body: str = Field(min_length=1, max_length=4000)
    actor: str = Field(default="operator", min_length=1, max_length=100)


class FeedbackCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: FeedbackVerdict
    corrected_importance: Literal["high", "medium", "low"] | None = None
    corrected_event_type: str | None = Field(default=None, max_length=100)
    comment: str | None = Field(default=None, max_length=4000)
    actor: str = Field(default="operator", min_length=1, max_length=100)


class IncidentMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_incident_ids: list[int] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _unique_sources(self) -> IncidentMergeRequest:
        if len(self.source_incident_ids) != len(set(self.source_incident_ids)):
            raise ValueError("source_incident_ids must be unique")
        return self


class IncidentSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_ids: list[int] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_events(self) -> IncidentSplitRequest:
        if len(self.event_ids) != len(set(self.event_ids)):
            raise ValueError("event_ids must be unique")
        return self


RemediationAction = Literal[
    "retry_outbox",
    "retry_dead_letters",
    "retry_stuck_events",
    "retry_incident_summaries",
    "test_enable_rule",
    "disable_rule",
    "acknowledge",
]


class RemediationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: RemediationAction
    resource_id: int | None = None
    resource_type: Literal["webhook_event", "incident"] | None = None
    batch_size: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def _require_resource(self) -> RemediationRequest:
        single_resource_actions = {"retry_outbox", "test_enable_rule", "disable_rule", "acknowledge"}
        if self.action in single_resource_actions and self.resource_id is None:
            raise ValueError("resource_id is required for this action")
        if self.action == "acknowledge" and self.resource_type is None:
            raise ValueError("resource_type is required for acknowledge")
        return self


class NoiseSuggestionApplyRequest(BaseModel):
    """Apply a currently valid noise-reduction recommendation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    suggestion_id: str = Field(min_length=1, max_length=160)
    window_days: int = Field(default=7, ge=1, le=90)
    actor: str = Field(default="operator", min_length=1, max_length=100)


class NoiseActionUndoRequest(BaseModel):
    """Undo a previously applied noise-reduction action."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str = Field(default="operator", min_length=1, max_length=100)


IntegrationTemplate = Literal["feishu", "generic_webhook", "openclaw"]


class IntegrationTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    template_id: IntegrationTemplate
    name: str = Field(default="Integration test", min_length=1, max_length=100)
    target_url: str = Field(default="", max_length=500)


class IntegrationSetupRequest(IntegrationTestRequest):
    enabled: bool = True
    priority: int = Field(default=10, ge=-1_000_000, le=1_000_000)
    source: str = Field(default="", max_length=200)
    importance: str = Field(default="", max_length=50)
    project: str = Field(default="", max_length=200)
    environment: str = Field(default="", max_length=200)
    target_name: str = Field(default="", max_length=100)
