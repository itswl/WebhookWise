from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import APIResponse


class SandboxTestRequest(BaseModel):
    """Request body for the webhook payload test sandbox (dry-run)."""

    model_config = ConfigDict(extra="forbid")

    # The source hint, exactly as a real webhook would pass it (path/header).
    # Free text: known sources route to their adapter, anything else falls back
    # to passthrough — both are valid things to test.
    source: str = Field(default="", max_length=100)
    # The raw alert payload, as a JSON object.
    payload: dict[str, Any]


class SandboxSource(BaseModel):
    input: str
    resolved: str
    adapter: str
    matched: bool


class SandboxRuleBasedAnalysis(BaseModel):
    importance: str
    event_type: str
    summary: str | None = None
    note: str


class SandboxForwarding(BaseModel):
    should_forward: bool
    skip_code: str
    skip_reason: str | None = None
    matched_rules: list[dict[str, Any]] = []
    silenced_by: dict[str, Any] | None = None


class SandboxTestData(BaseModel):
    """What WebhookWise would extract and decide for a pasted payload."""

    source: SandboxSource
    alert_hash: str
    dedup_key: str
    identity: dict[str, Any] = {}
    resources: list[Any] = []
    metrics: list[Any] = []
    match_fields: dict[str, Any] = {}
    rule_based_analysis: SandboxRuleBasedAnalysis
    forwarding: SandboxForwarding
    dedup_note: str


class SandboxTestResponse(APIResponse[SandboxTestData]):
    """Webhook payload dry-run response."""
