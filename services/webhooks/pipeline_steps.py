"""Processing steps used by the webhook pipeline entrypoints."""

import time
from dataclasses import dataclass
from typing import Any

from core.alert_concurrency import alert_processing_gate
from core.log_context import set_log_context
from core.logger import get_logger
from core.observability.events import emit_event
from core.observability.metrics import (
    WEBHOOK_PIPELINE_STEP_DURATION_SECONDS,
    WEBHOOK_PIPELINE_STEP_TOTAL,
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_STORM_SUPPRESSED_TOTAL,
)
from core.observability.signals import record_signal
from core.observability.tracing import span as otel_span
from services.webhooks.analysis_resolution import resolve_analysis
from services.webhooks.command_service import SaveWebhookResult
from services.webhooks.decisioning import ForwardingPolicy, build_final_analysis, normalize_importance
from services.webhooks.forwarding_stage import finalize_analysis_transaction
from services.webhooks.noise_stage import compute_noise
from services.webhooks.policies import AnalysisResolutionPolicy, NoiseReductionPolicy
from services.webhooks.types import (
    ForwardDecision,
    NoiseReductionContext,
    WebhookProcessContext,
)

logger = get_logger("webhooks.pipeline_steps")


@dataclass(frozen=True, slots=True)
class WebhookPipelineDependencies:
    analysis_policy: AnalysisResolutionPolicy | None = None
    noise_policy: NoiseReductionPolicy | None = None
    forwarding_policy: ForwardingPolicy | None = None
    http_client: Any | None = None


@dataclass(frozen=True, slots=True)
class PipelineProcessingResult:
    suppressed: bool
    save_result: SaveWebhookResult | None = None
    forward_decision: ForwardDecision | None = None
    noise: NoiseReductionContext | None = None
    final_analysis: dict[str, Any] | None = None
    outbox_count: int = 0


async def _handle_storm_suppression(ctx: WebhookProcessContext, lock_res: object) -> bool:
    if not getattr(lock_res, "suppressed", False):
        return False
    logger.info(
        "[Pipeline] 告警风暴背压抑制 event_id=%s request_id=%s queue_size=%s reason=%s",
        ctx.event_id,
        ctx.request_id,
        getattr(lock_res, "queue_size", 0),
        getattr(lock_res, "reason", "") or "alert_storm_backpressure",
    )
    WEBHOOK_STORM_SUPPRESSED_TOTAL.labels(source=ctx.metric_source).inc()
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="suppressed").inc()
    attrs = {
        "event_id": ctx.event_id or 0,
        "source": ctx.req_ctx.source,
        "alert_hash": ctx.alert_hash[:12],
        "webhook.status": "suppressed",
        "webhook.suppression.reason": getattr(lock_res, "reason", "") or "alert_storm_backpressure",
        "queue.depth": getattr(lock_res, "queue_size", 0),
    }
    emit_event("webhook.storm.suppressed", attrs)
    record_signal("webhook.ingest", "suppressed", attrs)
    return True


async def _resolve_noise_context(
    ctx: WebhookProcessContext,
    dependencies: WebhookPipelineDependencies,
) -> tuple[dict[str, Any], NoiseReductionContext, Any]:
    started = time.perf_counter()
    outcome = "success"
    with otel_span(
        "webhook.analyze",
        {
            "event_id": ctx.event_id or 0,
            "source": ctx.req_ctx.source,
            "alert_hash": ctx.alert_hash[:12],
            "pipeline.step": "analysis",
        },
    ):
        try:
            analysis_res = await resolve_analysis(
                ctx.alert_hash,
                ctx.req_ctx.webhook_full_data,
                policy=dependencies.analysis_policy,
                http_client=dependencies.http_client,
            )
        except Exception:
            outcome = "error"
            raise
        finally:
            WEBHOOK_PIPELINE_STEP_TOTAL.labels("analysis", ctx.metric_source, outcome).inc()
            WEBHOOK_PIPELINE_STEP_DURATION_SECONDS.labels("analysis", ctx.metric_source, outcome).observe(
                time.perf_counter() - started
            )
    route_type = analysis_res.analysis_result.get("_route_type", "ai")
    importance = normalize_importance(analysis_res.analysis_result.get("importance", "unknown"))
    set_log_context(route_type=route_type)

    if analysis_res.is_reused:
        logger.info(
            "[Pipeline] 分析结果复用(redis) event_id=%s request_id=%s importance=%s",
            ctx.event_id,
            ctx.request_id,
            importance,
        )
        emit_event(
            "webhook.analysis.reused",
            {
                "event_id": ctx.event_id or 0,
                "source": ctx.req_ctx.source,
                "alert_hash": ctx.alert_hash[:12],
                "importance": importance,
            },
        )
        return (
            analysis_res.analysis_result,
            NoiseReductionContext("standalone", None, 0.0, False, "缓存复用路径", 0, []),
            analysis_res,
        )

    logger.info(
        "[Pipeline] 分析完成 event_id=%s request_id=%s route=%s importance=%s degraded=%s",
        ctx.event_id,
        ctx.request_id,
        route_type,
        importance,
        analysis_res.analysis_result.get("_degraded", False),
    )
    emit_event(
        "webhook.analysis.completed",
        {
            "event_id": ctx.event_id or 0,
            "source": ctx.req_ctx.source,
            "alert_hash": ctx.alert_hash[:12],
            "importance": importance,
            "webhook.route": route_type,
            "ai.degraded": bool(analysis_res.analysis_result.get("_degraded", False)),
        },
    )
    noise_started = time.perf_counter()
    noise_outcome = "success"
    with otel_span(
        "webhook.noise",
        {
            "event_id": ctx.event_id or 0,
            "source": ctx.req_ctx.source,
            "alert_hash": ctx.alert_hash[:12],
            "pipeline.step": "noise",
        },
    ):
        try:
            noise = await compute_noise(
                ctx.alert_hash,
                ctx.req_ctx.source,
                ctx.req_ctx.parsed_data,
                analysis_res.analysis_result,
                policy=dependencies.noise_policy,
            )
        except Exception:
            noise_outcome = "error"
            raise
        finally:
            WEBHOOK_PIPELINE_STEP_TOTAL.labels("noise", ctx.metric_source, noise_outcome).inc()
            WEBHOOK_PIPELINE_STEP_DURATION_SECONDS.labels("noise", ctx.metric_source, noise_outcome).observe(
                time.perf_counter() - noise_started
            )
    return analysis_res.analysis_result, noise, analysis_res


async def run_processing_steps(
    ctx: WebhookProcessContext,
    dependencies: WebhookPipelineDependencies,
) -> PipelineProcessingResult:
    async with alert_processing_gate(ctx.alert_hash) as gate_res:
        validation_started = time.perf_counter()
        validation_outcome = "success"
        with otel_span(
            "webhook.validate",
            {
                "event_id": ctx.event_id or 0,
                "source": ctx.req_ctx.source,
                "alert_hash": ctx.alert_hash[:12],
                "pipeline.step": "validate",
            },
        ):
            try:
                if await _handle_storm_suppression(ctx, gate_res):
                    validation_outcome = "suppressed"
                    return PipelineProcessingResult(suppressed=True)
            except Exception:
                validation_outcome = "error"
                raise
            finally:
                WEBHOOK_PIPELINE_STEP_TOTAL.labels("validate", ctx.metric_source, validation_outcome).inc()
                WEBHOOK_PIPELINE_STEP_DURATION_SECONDS.labels(
                    "validate", ctx.metric_source, validation_outcome
                ).observe(time.perf_counter() - validation_started)

        analysis, noise, analysis_res = await _resolve_noise_context(ctx, dependencies)
        final_analysis = build_final_analysis(analysis, noise)
        persist_started = time.perf_counter()
        persist_outcome = "success"
        try:
            finalize_res = await finalize_analysis_transaction(
                ctx,
                analysis_res,
                final_analysis,
                noise,
                forwarding_policy=dependencies.forwarding_policy,
            )
        except Exception:
            persist_outcome = "error"
            raise
        finally:
            WEBHOOK_PIPELINE_STEP_TOTAL.labels("persist", ctx.metric_source, persist_outcome).inc()
            WEBHOOK_PIPELINE_STEP_DURATION_SECONDS.labels("persist", ctx.metric_source, persist_outcome).observe(
                time.perf_counter() - persist_started
            )
        return PipelineProcessingResult(
            suppressed=False,
            save_result=finalize_res.save_result,
            forward_decision=finalize_res.forward_decision,
            noise=noise,
            final_analysis=final_analysis,
            outbox_count=len(finalize_res.outbox_ids),
        )
