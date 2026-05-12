"""Webhook 处理主管线。

Coordinates persisted webhook events through parsing, analysis, noise
reduction, final state transition and forwarding intent creation.
"""

import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from core.alert_concurrency import alert_processing_gate
from core.log_context import clear_log_context, set_log_context
from core.logger import logger
from core.metrics import (
    WEBHOOK_NOISE_REDUCED_TOTAL,
    WEBHOOK_PROCESSING_DURATION_SECONDS,
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    WEBHOOK_STORM_SUPPRESSED_TOTAL,
    sanitize_source,
)
from core.otel import span as otel_span
from core.trace import generate_trace_id, set_trace_id
from models import WebhookEvent
from services.forwarding.outbox import schedule_forward_outbox_many
from services.webhooks.analysis_resolution import resolve_analysis
from services.webhooks.command_service import mark_webhook_suppressed
from services.webhooks.decisioning import ForwardingPolicy, build_final_analysis, normalize_importance
from services.webhooks.failure_handling import DeadLetterNotifier, handle_process_exception
from services.webhooks.forwarding_stage import finalize_analysis_transaction
from services.webhooks.noise_stage import compute_noise
from services.webhooks.policies import AnalysisResolutionPolicy, NoiseReductionPolicy, WebhookFailurePolicy
from services.webhooks.repository import transition_to_analyzing_and_load
from services.webhooks.request_parser import parse_request
from services.webhooks.types import (
    NoiseReductionContext,
    WebhookProcessContext,
    WebhookProcessingStatus,
)

# ── 主入口 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WebhookPipelineDependencies:
    analysis_policy: AnalysisResolutionPolicy | None = None
    noise_policy: NoiseReductionPolicy | None = None
    forwarding_policy: ForwardingPolicy | None = None
    failure_policy: WebhookFailurePolicy | None = None
    dead_letter_notifier: DeadLetterNotifier | None = None


async def _handle_storm_suppression(ctx: WebhookProcessContext, lock_res: object) -> bool:
    if not getattr(lock_res, "suppressed", False):
        return False
    logger.info(
        "[Pipeline] 告警风暴背压抑制 event_id=%s queue_size=%s reason=%s",
        ctx.event_id,
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
    await mark_webhook_suppressed(
        event_id=ctx.event_id,
        data=ctx.req_ctx.parsed_data,
        source=ctx.req_ctx.source,
        raw_payload=ctx.req_ctx.payload,
        headers=ctx.req_ctx.headers,
        client_ip=ctx.req_ctx.client_ip,
        ai_analysis={"noise_reduction": noise.__dict__},
        alert_hash=ctx.alert_hash,
    )
    return True


async def handle_webhook_process(
    event_id: int,
    client_ip: str = "",
    session: AsyncSession | None = None,
    *,
    dependencies: WebhookPipelineDependencies | None = None,
) -> None:
    set_trace_id(generate_trace_id(event_id=event_id))
    # 若 OTEL 已启用，优先用当前活动 span 的 trace_id 保证日志与 APM 一致
    from core.otel import get_otel_trace_id

    otel_tid = get_otel_trace_id()
    if otel_tid:
        set_trace_id(otel_tid)
    clear_log_context()
    set_log_context(event_id=event_id)
    await _handle_webhook_process_inner(
        event_id,
        client_ip,
        session=session,
        dependencies=dependencies or WebhookPipelineDependencies(),
    )


async def _handle_webhook_process_inner(
    event_id: int,
    client_ip: str = "",
    session: AsyncSession | None = None,
    *,
    dependencies: WebhookPipelineDependencies,
) -> None:
    start_perf = time.perf_counter()
    outcome, metric_source = "unknown", "unknown"
    with otel_span("webhook.process", {"event_id": event_id}) as _span:
        try:
            env = await transition_to_analyzing_and_load(event_id)
            if not env:
                logger.debug("[Pipeline] 忽略已处理或不存在的事件: event_id=%s", event_id)
                return

            metric_source = sanitize_source(env.source or "")
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="analyzing").inc()
            WEBHOOK_RECEIVED_TOTAL.labels(source=metric_source, status="received").inc()

            req_ctx = parse_request(client_ip, env.headers, env.payload or {}, env.raw_body, env.source, env.event_ts)
            alert_hash = WebhookEvent.generate_hash(req_ctx.parsed_data, req_ctx.source)
            set_log_context(alert_hash=alert_hash, source=req_ctx.source or "unknown")
            ctx = WebhookProcessContext(
                event_id=event_id,
                client_ip=client_ip,
                metric_source=metric_source,
                req_ctx=req_ctx,
                alert_hash=alert_hash,
            )
            logger.info(
                "[Pipeline] 开始处理 event_id=%s source=%s adapter=%s",
                event_id,
                req_ctx.source,
                req_ctx.parsed_data.get("_adapter", req_ctx.source),
            )
            if _span:
                _span.set_attribute("source", req_ctx.source or "unknown")
                _span.set_attribute("alert_hash", alert_hash[:12])

            async with alert_processing_gate(alert_hash) as gate_res:
                if await _handle_storm_suppression(ctx, gate_res):
                    outcome = "suppressed"
                    return

                analysis_res = await resolve_analysis(
                    alert_hash, req_ctx.webhook_full_data, policy=dependencies.analysis_policy
                )
                route_type = analysis_res.analysis_result.get("_route_type", "ai")
                importance = normalize_importance(analysis_res.analysis_result.get("importance", "unknown"))
                if analysis_res.is_reused:
                    set_log_context(route_type=route_type)
                    logger.info("[Pipeline] 分析结果复用(redis) event_id=%s importance=%s", event_id, importance)
                    noise = NoiseReductionContext("standalone", None, 0.0, False, "缓存复用路径", 0, [])
                else:
                    set_log_context(route_type=route_type)
                    logger.info(
                        "[Pipeline] 分析完成 event_id=%s route=%s importance=%s degraded=%s",
                        event_id,
                        route_type,
                        importance,
                        analysis_res.analysis_result.get("_degraded", False),
                    )
                    noise = await compute_noise(
                        alert_hash,
                        req_ctx.source,
                        req_ctx.parsed_data,
                        analysis_res.analysis_result,
                        policy=dependencies.noise_policy,
                    )

                final_analysis = build_final_analysis(analysis_res.analysis_result, noise)
                save_res, fwd_dec, outbox_ids = await finalize_analysis_transaction(
                    ctx,
                    analysis_res,
                    final_analysis,
                    noise,
                    forwarding_policy=dependencies.forwarding_policy,
                )
            await schedule_forward_outbox_many(outbox_ids)

            event_type = (
                "beyond_window" if save_res.beyond_window else ("duplicate" if save_res.is_duplicate else "new")
            )
            importance = normalize_importance(final_analysis.get("importance", "unknown"))
            route_label = final_analysis.get("_route_type", "ai")
            noise_relation = noise.relation if noise else "unknown"
            duration_ms = int((time.perf_counter() - start_perf) * 1000)
            if fwd_dec is None:
                fwd_info = " forward=unknown"
            elif fwd_dec.should_forward:
                fwd_info = f" forward=queued rules={len(fwd_dec.matched_rules)} outbox={len(outbox_ids)}"
                if fwd_dec.is_periodic_reminder:
                    fwd_info += "(periodic)"
            else:
                fwd_info = f" forward=no skip={fwd_dec.skip_reason or 'unknown'}"
            logger.info(
                "[Pipeline] 处理完成 event_id=%s type=%s importance=%s route=%s noise=%s%s duration=%dms",
                event_id,
                event_type,
                importance,
                route_label,
                noise_relation,
                fwd_info,
                duration_ms,
            )

            if _span:
                _span.set_attribute("importance", importance)
                _span.set_attribute("route", route_label)
                _span.set_attribute("event_type", event_type)
                _span.set_attribute("duration_ms", duration_ms)

            WEBHOOK_NOISE_REDUCED_TOTAL.labels(
                source=metric_source,
                relation=noise_relation,
                suppressed=str(noise.suppress_forward).lower() if noise else "false",
            ).inc()

            outcome = "completed"
            set_log_context(processing_status=WebhookProcessingStatus.COMPLETED.value)
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="completed").inc()

        except Exception as e:
            outcome = await handle_process_exception(
                event_id,
                e,
                _span,
                policy=dependencies.failure_policy,
                dead_letter_notifier=dependencies.dead_letter_notifier,
            )
            return
        finally:
            duration = time.perf_counter() - start_perf
            WEBHOOK_PROCESSING_DURATION_SECONDS.labels(source=metric_source, outcome=outcome).observe(duration)
