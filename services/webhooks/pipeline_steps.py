"""Processing steps used by the webhook pipeline entrypoints."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.alert_concurrency import alert_processing_gate
from core.log_context import set_log_context
from core.logger import logger
from core.metrics import WEBHOOK_PROCESSING_STATUS_TOTAL, WEBHOOK_STORM_SUPPRESSED_TOTAL
from services.webhooks.analysis_resolution import resolve_analysis
from services.webhooks.command_service import SaveWebhookResult, mark_webhook_suppressed
from services.webhooks.decisioning import ForwardingPolicy, build_final_analysis, normalize_importance
from services.webhooks.forwarding_stage import finalize_analysis_transaction
from services.webhooks.noise_stage import compute_noise
from services.webhooks.policies import AnalysisResolutionPolicy, NoiseReductionPolicy, WebhookFailurePolicy
from services.webhooks.types import (
    ForwardDecision,
    NoiseReductionContext,
    WebhookProcessContext,
)

if TYPE_CHECKING:
    from services.webhooks.failure_handling import DeadLetterNotifier
    from services.webhooks.forwarding_stage import ForwardingClient


@dataclass(frozen=True, slots=True)
class WebhookPipelineDependencies:
    analysis_policy: AnalysisResolutionPolicy | None = None
    noise_policy: NoiseReductionPolicy | None = None
    forwarding_policy: ForwardingPolicy | None = None
    failure_policy: WebhookFailurePolicy | None = None
    dead_letter_notifier: "DeadLetterNotifier | None" = None
    http_client: Any | None = None
    forwarding_client: "ForwardingClient | None" = None


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
    noise = NoiseReductionContext(
        "storm",
        None,
        1.0,
        True,
        getattr(lock_res, "reason", "") or "alert_storm_backpressure",
        getattr(lock_res, "queue_size", 0),
        [],
    )
    if ctx.event_id is not None:
        await mark_webhook_suppressed(
            event_id=ctx.event_id,
            request_id=ctx.request_id,
            data=ctx.req_ctx.parsed_data,
            source=ctx.req_ctx.source,
            raw_payload=ctx.req_ctx.payload,
            headers=ctx.req_ctx.headers,
            client_ip=ctx.req_ctx.client_ip,
            ai_analysis={"noise_reduction": noise.__dict__},
            alert_hash=ctx.alert_hash,
        )
    return True


async def _resolve_noise_context(
    ctx: WebhookProcessContext,
    dependencies: WebhookPipelineDependencies,
) -> tuple[dict[str, Any], NoiseReductionContext, Any]:
    analysis_res = await resolve_analysis(
        ctx.alert_hash,
        ctx.req_ctx.webhook_full_data,
        policy=dependencies.analysis_policy,
        http_client=dependencies.http_client,
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
    noise = await compute_noise(
        ctx.alert_hash,
        ctx.req_ctx.source,
        ctx.req_ctx.parsed_data,
        analysis_res.analysis_result,
        policy=dependencies.noise_policy,
    )
    return analysis_res.analysis_result, noise, analysis_res


async def run_processing_steps(
    ctx: WebhookProcessContext,
    dependencies: WebhookPipelineDependencies,
) -> PipelineProcessingResult:
    async with alert_processing_gate(ctx.alert_hash) as gate_res:
        if await _handle_storm_suppression(ctx, gate_res):
            return PipelineProcessingResult(suppressed=True)

        analysis, noise, analysis_res = await _resolve_noise_context(ctx, dependencies)
        final_analysis = build_final_analysis(analysis, noise)
        finalize_res = await finalize_analysis_transaction(
            ctx,
            analysis_res,
            final_analysis,
            noise,
            forwarding_policy=dependencies.forwarding_policy,
        )
        return PipelineProcessingResult(
            suppressed=False,
            save_result=finalize_res.save_result,
            forward_decision=finalize_res.forward_decision,
            noise=noise,
            final_analysis=final_analysis,
            outbox_count=len(finalize_res.outbox_ids),
        )
