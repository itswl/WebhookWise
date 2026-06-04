"""Shared runtime primitives for webhook pipeline execution."""

from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from core.observability.attributes import (
    WEBHOOK_ALERT_HASH,
    WEBHOOK_EVENT_ID,
    WEBHOOK_OUTCOME,
    WEBHOOK_SOURCE,
)
from core.observability.metrics import (
    WEBHOOK_PIPELINE_STEP_DURATION_SECONDS,
    WEBHOOK_PIPELINE_STEP_TOTAL,
)
from core.observability.tracing import add_span_event_to, otel_span, set_span_ok
from services.webhooks.command_service import SaveWebhookResult
from services.webhooks.decisioning import ForwardDecision, ForwardingPolicy
from services.webhooks.policies import NoiseReductionPolicy
from services.webhooks.types import AnalysisResult, NoiseReductionContext, WebhookProcessContext


@dataclass(frozen=True, slots=True)
class WebhookPipelineDependencies:
    dedup_window_seconds: int
    noise_policy: NoiseReductionPolicy | None = None
    forwarding_policy: ForwardingPolicy | None = None
    http_client: Any | None = None

    @property
    def dedup_ttl_seconds(self) -> int:
        return max(60, int(self.dedup_window_seconds) * 2)


@dataclass(frozen=True, slots=True)
class PipelineProcessingResult:
    suppressed: bool
    save_result: SaveWebhookResult | None = None
    forward_decision: ForwardDecision | None = None
    noise: NoiseReductionContext | None = None
    final_analysis: AnalysisResult | None = None
    outbox_count: int = 0


def record_step_metrics(step: str, metric_source: str, outcome: str, started: float) -> None:
    WEBHOOK_PIPELINE_STEP_TOTAL.labels(step, metric_source, outcome).inc()
    WEBHOOK_PIPELINE_STEP_DURATION_SECONDS.labels(step, metric_source, outcome).observe(time.perf_counter() - started)


_PIPELINE_SPAN_NAMES = {
    "parse": "webhook.parse",
    "validate": "webhook.validate",
    "dedup": "webhook.dedup",
    "analysis": "webhook.analyze",
    "noise": "webhook.noise",
}


def _step_attrs(ctx: WebhookProcessContext, step: str) -> dict[str, Any]:
    return {
        WEBHOOK_EVENT_ID: ctx.event_id or 0,
        WEBHOOK_SOURCE: ctx.req_ctx.source,
        WEBHOOK_ALERT_HASH: ctx.alert_hash[:12],
        "pipeline.step": step,
    }


@asynccontextmanager
async def _instrument_step(
    *,
    step_name: str,
    metric_source: str,
    span_attrs: dict[str, Any],
) -> Any:
    started = time.perf_counter()
    outcome: dict[str, str] = {"value": "success"}
    with otel_span(_PIPELINE_SPAN_NAMES[step_name], span_attrs) as span:
        try:
            yield span, outcome
        finally:
            if sys.exception() is not None:
                outcome["value"] = "error"
            duration = time.perf_counter() - started
            if span is not None:
                span.set_attribute(WEBHOOK_OUTCOME, outcome["value"])
                span.set_attribute("pipeline.step", step_name)
                span.set_attribute("pipeline.step.duration_ms", int(duration * 1000))
                add_span_event_to(
                    span,
                    "webhook.pipeline.step.completed",
                    {
                        "pipeline.step": step_name,
                        WEBHOOK_OUTCOME: outcome["value"],
                        "pipeline.step.duration_ms": int(duration * 1000),
                    },
                )
                if outcome["value"] != "error":
                    set_span_ok(span)
            record_step_metrics(step_name, metric_source, outcome["value"], started)


@asynccontextmanager
async def pipeline_step(ctx: WebhookProcessContext, step_name: str) -> Any:
    async with _instrument_step(
        step_name=step_name,
        metric_source=ctx.metric_source,
        span_attrs=_step_attrs(ctx, step_name),
    ) as state:
        yield state


@asynccontextmanager
async def ingest_step(*, step_name: str, metric_source: str, source: str | None) -> Any:
    async with _instrument_step(
        step_name=step_name,
        metric_source=metric_source,
        span_attrs={
            WEBHOOK_SOURCE: source or "unknown",
            WEBHOOK_EVENT_ID: 0,
            "pipeline.step": step_name,
        },
    ) as state:
        yield state
