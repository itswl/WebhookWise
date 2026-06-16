"""Webhook pipeline stage implementations."""

from __future__ import annotations

import time
from typing import Any, cast

from core.alert_concurrency import ProcessingLockLost
from core.log_context import set_log_context
from core.logger import get_logger
from core.observability.attributes import (
    WEBHOOK_ALERT_HASH,
    WEBHOOK_EVENT_ID,
    WEBHOOK_IMPORTANCE,
    WEBHOOK_ROUTE,
    WEBHOOK_SOURCE,
)
from core.observability.events import emit_event, record_signal
from core.observability.metrics import (
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_STORM_SUPPRESSED_TOTAL,
)
from services.analysis.ai_usage import log_ai_usage
from services.dedup import DedupResult, remember_dedup_state, resolve_dedup
from services.forwarding.outbox import schedule_forward_outbox_many
from services.webhooks import pipeline_runtime
from services.webhooks.decisioning import build_final_analysis, normalize_importance
from services.webhooks.forwarding_stage import finalize_analysis_transaction
from services.webhooks.noise_stage import compute_noise
from services.webhooks.types import (
    AnalysisResult,
    NoiseReductionContext,
    WebhookProcessContext,
    analysis_route,
    is_analysis_degraded,
    pending_dedup_placeholder,
    set_analysis_route,
)

logger = get_logger("webhooks.pipeline")


async def _handle_reused_analysis(
    ctx: WebhookProcessContext,
    dedup_result: DedupResult,
) -> tuple[AnalysisResult, NoiseReductionContext, DedupResult]:
    analysis: AnalysisResult = cast(AnalysisResult, dedup_result.analysis or {})
    route_type = dedup_result.route_type or "redis_reuse"
    set_analysis_route(analysis, route_type)
    importance = normalize_importance(analysis.get("importance", "unknown"))
    set_log_context(webhook_route=route_type)
    await log_ai_usage(route_type, ctx.alert_hash, ctx.req_ctx.source, cache_hit=True)
    logger.debug(
        "[Pipeline] Analysis result reused event_id=%s request_id=%s importance=%s route=%s",
        ctx.event_id,
        ctx.request_id,
        importance,
        route_type,
    )
    emit_event(
        "webhook.analysis.reused",
        {
            WEBHOOK_EVENT_ID: ctx.event_id or 0,
            WEBHOOK_SOURCE: ctx.req_ctx.source,
            WEBHOOK_ALERT_HASH: ctx.alert_hash[:12],
            WEBHOOK_IMPORTANCE: importance,
        },
    )
    return (
        analysis,
        NoiseReductionContext("standalone", None, 0.0, False, "Cache reuse path", 0, ()),
        dedup_result,
    )


async def _run_fresh_analysis(
    ctx: WebhookProcessContext,
    dedup_result: DedupResult,
    dependencies: pipeline_runtime.WebhookPipelineDependencies,
) -> AnalysisResult:
    async with pipeline_runtime.pipeline_step(ctx, "analysis") as (_span, _outcome):
        from services.analysis.ai_analyzer import analyze_webhook_with_ai

        await remember_dedup_state(
            ctx.dedup_key,
            original_event_id=0,
            analysis=pending_dedup_placeholder(),
            ttl_seconds=dependencies.dedup_ttl_seconds,
            reset_chain=dedup_result.reset_chain,
        )

        return await analyze_webhook_with_ai(
            ctx.req_ctx.webhook_full_data,
            http_client=dependencies.http_client,
        )


def _record_analysis_completed(ctx: WebhookProcessContext, analysis_result: AnalysisResult) -> None:
    route_type = analysis_route(analysis_result)
    importance = normalize_importance(analysis_result.get("importance", "unknown"))
    set_log_context(webhook_route=route_type)
    logger.info(
        "[Pipeline] Analysis completed event_id=%s request_id=%s route=%s importance=%s degraded=%s",
        ctx.event_id,
        ctx.request_id,
        route_type,
        importance,
        is_analysis_degraded(analysis_result),
    )
    emit_event(
        "webhook.analysis.completed",
        {
            WEBHOOK_EVENT_ID: ctx.event_id or 0,
            WEBHOOK_SOURCE: ctx.req_ctx.source,
            WEBHOOK_ALERT_HASH: ctx.alert_hash[:12],
            WEBHOOK_IMPORTANCE: importance,
            WEBHOOK_ROUTE: route_type,
            "ai.degraded": is_analysis_degraded(analysis_result),
        },
    )


async def resolve_noise_context(
    ctx: WebhookProcessContext, dependencies: pipeline_runtime.WebhookPipelineDependencies
) -> tuple[AnalysisResult, NoiseReductionContext, DedupResult]:
    dedup_action = "error"
    async with pipeline_runtime.pipeline_step(ctx, "dedup") as (dedup_span, _outcome):
        try:
            dedup_result = await resolve_dedup(ctx.dedup_key)
            dedup_action = str(dedup_result.action)
        finally:
            if dedup_span is not None:
                dedup_span.set_attribute("dedup.action", dedup_action)

    if dedup_result.action in ("reuse", "rechain"):
        return await _handle_reused_analysis(ctx, dedup_result)

    analysis_result = await _run_fresh_analysis(ctx, dedup_result, dependencies)
    _record_analysis_completed(ctx, analysis_result)

    async with pipeline_runtime.pipeline_step(ctx, "noise") as (_span, _outcome):
        noise = await compute_noise(
            ctx.alert_hash,
            ctx.req_ctx.source,
            dict(ctx.req_ctx.parsed_data),
            analysis_result,
            policy=dependencies.noise_policy,
        )

    return analysis_result, noise, dedup_result


def _suppression_attrs(ctx: WebhookProcessContext, gate_res: Any) -> dict[str, Any]:
    return {
        WEBHOOK_EVENT_ID: ctx.event_id or 0,
        WEBHOOK_SOURCE: ctx.req_ctx.source,
        WEBHOOK_ALERT_HASH: ctx.alert_hash[:12],
        "webhook.status": "suppressed",
        "webhook.suppression.reason": getattr(gate_res, "reason", "") or "alert_storm_backpressure",
        "queue.depth": getattr(gate_res, "queue_size", 0),
    }


def _record_suppressed(ctx: WebhookProcessContext, gate_res: Any) -> None:
    logger.info(
        "[Pipeline] Alert storm backpressure suppression event_id=%s request_id=%s queue_size=%s reason=%s",
        ctx.event_id,
        ctx.request_id,
        getattr(gate_res, "queue_size", 0),
        getattr(gate_res, "reason", "") or "alert_storm_backpressure",
    )
    WEBHOOK_STORM_SUPPRESSED_TOTAL.labels(source=ctx.metric_source).inc()
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="suppressed").inc()
    attrs = _suppression_attrs(ctx, gate_res)
    emit_event("webhook.storm.suppressed", attrs)
    record_signal("webhook.ingest", "suppressed", attrs)


async def validate_backpressure(
    ctx: WebhookProcessContext, gate_res: Any
) -> pipeline_runtime.PipelineProcessingResult | None:
    async with pipeline_runtime.pipeline_step(ctx, "validate") as (_span, outcome):
        if not getattr(gate_res, "suppressed", False):
            return None
        reason = getattr(gate_res, "reason", "") or "alert_storm_backpressure"
        # Distinguish genuine storm backpressure (intentional load-shed: drop) from
        # a Redis outage. When the gate suppresses because Redis is unavailable,
        # dropping the event would turn a Redis blip into silent alert loss. Raise
        # a retryable error instead so the broker re-queues the webhook; once Redis
        # recovers the retry processes it normally.
        if reason == "redis_unavailable":
            outcome["value"] = "redis_unavailable"
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="redis_unavailable_requeue").inc()
            logger.warning(
                "[Pipeline] Redis unavailable caused backpressure suppression; requeueing instead of dropping request_id=%s dedup_key=%s",
                ctx.request_id,
                ctx.dedup_key[:12],
            )
            raise ProcessingLockLost(f"redis unavailable, requeue dedup_key={ctx.dedup_key[:12]}")
        _record_suppressed(ctx, gate_res)
        outcome["value"] = "suppressed"
        return pipeline_runtime.PipelineProcessingResult(suppressed=True)


async def persist_and_schedule(
    ctx: WebhookProcessContext,
    analysis: AnalysisResult,
    noise: NoiseReductionContext,
    analysis_res: DedupResult,
    dependencies: pipeline_runtime.WebhookPipelineDependencies,
    gate_res: Any | None = None,
) -> pipeline_runtime.PipelineProcessingResult:
    # If the distributed processing lock was lost while analysis was running,
    # another worker may already be processing this same dedup_key. Abort before
    # committing side-effects (persist + outbox) rather than racing it. Raising a
    # retryable error re-queues the webhook; on retry the dedup state short-
    # circuits to REUSE so the original is not duplicated.
    lock_lost = getattr(gate_res, "lock_lost", None)
    if lock_lost is not None and lock_lost.is_set():
        WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="lock_lost").inc()
        logger.warning(
            "[Pipeline] Processing lock lost mid-flight; abandoning commit and retrying request_id=%s dedup_key=%s",
            ctx.request_id,
            ctx.dedup_key[:12],
        )
        raise ProcessingLockLost(f"processing lock lost mid-flight dedup_key={ctx.dedup_key[:12]}")

    final_analysis = build_final_analysis(analysis, noise)
    persist_started = time.perf_counter()
    persist_outcome = "error"
    try:
        finalize_res = await finalize_analysis_transaction(
            ctx,
            analysis_res,
            final_analysis,
            noise,
            forwarding_policy=dependencies.forwarding_policy,
        )
        persist_outcome = "success"
    finally:
        pipeline_runtime.record_step_metrics("persist", ctx.metric_source, persist_outcome, persist_started)

    await remember_dedup_state(
        ctx.dedup_key,
        original_event_id=finalize_res.save_result.webhook_id,
        analysis=dict(final_analysis),
        ttl_seconds=dependencies.dedup_ttl_seconds,
        reset_chain=analysis_res.reset_chain or analysis_res.is_rechain,
    )
    await schedule_forward_outbox_many(finalize_res.outbox_ids)

    return pipeline_runtime.PipelineProcessingResult(
        suppressed=False,
        save_result=finalize_res.save_result,
        forward_decision=finalize_res.forward_decision,
        noise=noise,
        final_analysis=final_analysis,
        outbox_count=len(finalize_res.outbox_ids),
    )
