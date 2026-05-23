"""Webhook 处理主管线。

Coordinates raw webhook envelopes through parsing, analysis, noise reduction,
final persistence and forwarding intent creation.
"""

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core import json
from core.alert_concurrency import alert_processing_gate
from core.log_context import clear_log_context, set_log_context
from core.logger import get_logger
from core.observability.attributes import (
    WEBHOOK_ALERT_HASH,
    WEBHOOK_EVENT_TYPE,
    WEBHOOK_IMPORTANCE,
    WEBHOOK_OUTCOME,
    WEBHOOK_PROCESSING_DURATION_MS,
    WEBHOOK_ROUTE,
    WEBHOOK_SOURCE,
)
from core.observability.events import emit_event
from core.observability.metrics import (
    WEBHOOK_ANALYSIS_ROUTE_TOTAL,
    WEBHOOK_NOISE_REDUCED_TOTAL,
    WEBHOOK_PIPELINE_STEP_DURATION_SECONDS,
    WEBHOOK_PIPELINE_STEP_TOTAL,
    WEBHOOK_PROCESSING_DURATION_SECONDS,
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    WEBHOOK_STORM_SUPPRESSED_TOTAL,
    sanitize_source,
)
from core.observability.signals import record_signal
from core.observability.tracing import (
    generate_trace_id,
    get_current_trace_id,
    set_fallback_trace_id,
    set_span_error,
)
from core.observability.tracing import (
    span as otel_span,
)
from services.webhooks.analysis_resolution import resolve_analysis
from services.webhooks.command_service import SaveWebhookResult
from services.webhooks.decisioning import ForwardingPolicy, build_final_analysis, normalize_importance
from services.webhooks.forwarding_stage import finalize_analysis_transaction
from services.webhooks.identity import generate_alert_hash
from services.webhooks.noise_stage import compute_noise
from services.webhooks.policies import AnalysisResolutionPolicy, NoiseReductionPolicy
from services.webhooks.repository import EventEnvelope
from services.webhooks.request_parser import parse_request
from services.webhooks.types import (
    AnalysisResolution,
    AnalysisResult,
    ForwardDecision,
    NoiseReductionContext,
    WebhookProcessContext,
    WebhookProcessingStatus,
)

logger = get_logger("webhooks.pipeline")

# ── 主入口 ───────────────────────────────────────────────────────────────────


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
    final_analysis: AnalysisResult | None = None
    outbox_count: int = 0


def _record_step_metrics(step: str, metric_source: str, outcome: str, started: float) -> None:
    WEBHOOK_PIPELINE_STEP_TOTAL.labels(step, metric_source, outcome).inc()
    WEBHOOK_PIPELINE_STEP_DURATION_SECONDS.labels(step, metric_source, outcome).observe(time.perf_counter() - started)


@dataclass(slots=True)
class _StepContext:
    outcome: str = "success"


@asynccontextmanager
async def _pipeline_step(
    *,
    step_name: str,
    metric_source: str,
    span_name: str,
    span_attrs: dict[str, Any],
) -> Any:
    started = time.perf_counter()
    ctx = _StepContext()
    with otel_span(span_name, span_attrs) as span:
        try:
            yield span, ctx
        except Exception:
            ctx.outcome = "error"
            raise
        finally:
            _record_step_metrics(step_name, metric_source, ctx.outcome, started)



@dataclass(frozen=True, slots=True)
class _ProcessingRun:
    ctx: WebhookProcessContext
    dependencies: WebhookPipelineDependencies

    async def run(self) -> PipelineProcessingResult:
        async with alert_processing_gate(self.ctx.alert_hash) as gate_res:
            async with _pipeline_step(
                step_name="validate",
                metric_source=self.ctx.metric_source,
                span_name="webhook.validate",
                span_attrs={
                    "event_id": self.ctx.event_id or 0,
                    "source": self.ctx.req_ctx.source,
                    "alert_hash": self.ctx.alert_hash[:12],
                    "pipeline.step": "validate",
                },
            ) as (_span, step_ctx):
                if await self._handle_storm_suppression(gate_res):
                    step_ctx.outcome = "suppressed"
                    return PipelineProcessingResult(suppressed=True)

            analysis, noise, analysis_res = await self._resolve_noise_context()
            final_analysis = build_final_analysis(analysis, noise)
            persist_started = time.perf_counter()
            persist_outcome = "success"
            try:
                finalize_res = await finalize_analysis_transaction(
                    self.ctx,
                    analysis_res,
                    final_analysis,
                    noise,
                    forwarding_policy=self.dependencies.forwarding_policy,
                )
            except Exception:
                persist_outcome = "error"
                raise
            finally:
                _record_step_metrics("persist", self.ctx.metric_source, persist_outcome, persist_started)

            return PipelineProcessingResult(
                suppressed=False,
                save_result=finalize_res.save_result,
                forward_decision=finalize_res.forward_decision,
                noise=noise,
                final_analysis=final_analysis,
                outbox_count=len(finalize_res.outbox_ids),
            )

    async def _handle_storm_suppression(self, lock_res: object) -> bool:
        if not getattr(lock_res, "suppressed", False):
            return False
        logger.info(
            "[Pipeline] 告警风暴背压抑制 event_id=%s request_id=%s queue_size=%s reason=%s",
            self.ctx.event_id,
            self.ctx.request_id,
            getattr(lock_res, "queue_size", 0),
            getattr(lock_res, "reason", "") or "alert_storm_backpressure",
        )
        WEBHOOK_STORM_SUPPRESSED_TOTAL.labels(source=self.ctx.metric_source).inc()
        WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="suppressed").inc()
        attrs = {
            "event_id": self.ctx.event_id or 0,
            "source": self.ctx.req_ctx.source,
            "alert_hash": self.ctx.alert_hash[:12],
            "webhook.status": "suppressed",
            "webhook.suppression.reason": getattr(lock_res, "reason", "") or "alert_storm_backpressure",
            "queue.depth": getattr(lock_res, "queue_size", 0),
        }
        emit_event("webhook.storm.suppressed", attrs)
        record_signal("webhook.ingest", "suppressed", attrs)
        return True

    async def _resolve_noise_context(self) -> tuple[AnalysisResult, NoiseReductionContext, AnalysisResolution]:
        async with _pipeline_step(
            step_name="analysis",
            metric_source=self.ctx.metric_source,
            span_name="webhook.analyze",
            span_attrs={
                "event_id": self.ctx.event_id or 0,
                "source": self.ctx.req_ctx.source,
                "alert_hash": self.ctx.alert_hash[:12],
                "pipeline.step": "analysis",
            },
        ) as (_span, _step_ctx):
            analysis_res = await resolve_analysis(
                self.ctx.alert_hash,
                self.ctx.req_ctx.webhook_full_data,
                policy=self.dependencies.analysis_policy,
                http_client=self.dependencies.http_client,
            )

        route_type = analysis_res.analysis_result.get("_route_type", "ai")
        importance = normalize_importance(analysis_res.analysis_result.get("importance", "unknown"))
        set_log_context(route_type=route_type)
        WEBHOOK_ANALYSIS_ROUTE_TOTAL.labels(self.ctx.metric_source, route_type).inc()

        if analysis_res.is_reused:
            logger.debug(
                "[Pipeline] 分析结果复用(redis) event_id=%s request_id=%s importance=%s",
                self.ctx.event_id,
                self.ctx.request_id,
                importance,
            )
            emit_event(
                "webhook.analysis.reused",
                {
                    "event_id": self.ctx.event_id or 0,
                    "source": self.ctx.req_ctx.source,
                    "alert_hash": self.ctx.alert_hash[:12],
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
            self.ctx.event_id,
            self.ctx.request_id,
            route_type,
            importance,
            analysis_res.analysis_result.get("_degraded", False),
        )
        emit_event(
            "webhook.analysis.completed",
            {
                "event_id": self.ctx.event_id or 0,
                "source": self.ctx.req_ctx.source,
                "alert_hash": self.ctx.alert_hash[:12],
                "importance": importance,
                "webhook.route": route_type,
                "ai.degraded": bool(analysis_res.analysis_result.get("_degraded", False)),
            },
        )

        async with _pipeline_step(
            step_name="noise",
            metric_source=self.ctx.metric_source,
            span_name="webhook.noise",
            span_attrs={
                "event_id": self.ctx.event_id or 0,
                "source": self.ctx.req_ctx.source,
                "alert_hash": self.ctx.alert_hash[:12],
                "pipeline.step": "noise",
            },
        ) as (_span, _step_ctx):
            noise = await compute_noise(
                self.ctx.alert_hash,
                self.ctx.req_ctx.source,
                self.ctx.req_ctx.parsed_data,
                analysis_res.analysis_result,
                policy=self.dependencies.noise_policy,
            )

        return analysis_res.analysis_result, noise, analysis_res


async def handle_webhook_ingest(
    *,
    source: str,
    raw_headers: dict[str, Any],
    raw_body: str,
    client_ip: str = "",
    request_id: str | None = None,
    received_at: str | None = None,
    dependencies: WebhookPipelineDependencies | None = None,
) -> None:
    """Process a newly ingested webhook without pre-writing it to PostgreSQL."""
    trace_id = request_id or generate_trace_id()
    set_fallback_trace_id(trace_id)
    clear_log_context()
    set_log_context(request_id=trace_id, source=source)
    logger.info(
        "[Pipeline] raw ingest 开始 request_id=%s source=%s ip=%s body_size=%d received_at=%s",
        request_id,
        source,
        client_ip,
        len(raw_body.encode("utf-8")),
        received_at or "",
    )
    payload: dict[str, Any] | None = None
    if raw_body:
        try:
            loaded = json.loads(raw_body)
            payload = loaded if isinstance(loaded, dict) else None
        except Exception:
            payload = None

    env = EventEnvelope(
        headers=dict(raw_headers or {}),
        payload=payload,
        raw_body=raw_body.encode("utf-8"),
        source=source,
        event_ts=received_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        request_id=request_id,
    )
    await _handle_raw_ingest(
        env,
        client_ip,
        dependencies=dependencies or WebhookPipelineDependencies(),
    )


def _forwarding_log_info(result: PipelineProcessingResult) -> str:
    fwd_dec = result.forward_decision
    if fwd_dec is None:
        return " forward=unknown"
    if not fwd_dec.should_forward:
        return f" forward=no skip={fwd_dec.skip_reason or 'unknown'}"

    info = f" forward=queued rules={len(fwd_dec.matched_rules)} targets={result.outbox_count}"
    if result.outbox_count == 0:
        info = f" forward=no_target rules={len(fwd_dec.matched_rules)} targets=0"
    if fwd_dec.is_periodic_reminder:
        info += "(periodic)"
    return info


def _log_completed_processing(
    *,
    ctx: WebhookProcessContext,
    result: PipelineProcessingResult,
    request_id: str | None,
    start_perf: float,
    span: Any | None,
) -> None:
    save_res = result.save_result
    noise = result.noise
    final_analysis = result.final_analysis
    if save_res is None or noise is None or final_analysis is None:
        raise RuntimeError("completed pipeline result is missing final state")

    if ctx.event_id is None:
        set_log_context(event_id=save_res.webhook_id)
    logger.info(
        "[Pipeline] 告警已持久化 event_id=%s request_id=%s duplicate=%s original_id=%s beyond_window=%s",
        save_res.webhook_id,
        request_id,
        save_res.is_duplicate,
        save_res.original_id,
        save_res.beyond_window,
    )

    event_type = "beyond_window" if save_res.beyond_window else ("duplicate" if save_res.is_duplicate else "new")
    importance = normalize_importance(final_analysis.get("importance", "unknown"))
    route_label = final_analysis.get("_route_type", "ai")
    noise_relation = noise.relation
    duration_ms = int((time.perf_counter() - start_perf) * 1000)
    logger.info(
        "[Pipeline] 处理完成 event_id=%s request_id=%s type=%s importance=%s route=%s noise=%s%s duration=%dms",
        save_res.webhook_id,
        request_id,
        event_type,
        importance,
        route_label,
        noise_relation,
        _forwarding_log_info(result),
        duration_ms,
    )

    if span:
        span.set_attribute(WEBHOOK_IMPORTANCE, importance)
        span.set_attribute(WEBHOOK_ROUTE, route_label)
        span.set_attribute(WEBHOOK_EVENT_TYPE, event_type)
        span.set_attribute(WEBHOOK_PROCESSING_DURATION_MS, duration_ms)

    WEBHOOK_NOISE_REDUCED_TOTAL.labels(
        source=ctx.metric_source,
        relation=noise_relation,
        suppressed=str(noise.suppress_forward).lower(),
    ).inc()


def _record_pipeline_duration(metric_source: str, outcome: str, start_perf: float, span: Any | None) -> None:
    duration = time.perf_counter() - start_perf
    if span:
        span.set_attribute(WEBHOOK_OUTCOME, outcome)
    WEBHOOK_PROCESSING_DURATION_SECONDS.labels(source=metric_source, outcome=outcome).observe(duration)


async def _handle_raw_ingest(
    envelope: EventEnvelope,
    client_ip: str = "",
    *,
    dependencies: WebhookPipelineDependencies,
) -> None:
    start_perf = time.perf_counter()
    outcome = "unknown"
    metric_source = sanitize_source(envelope.source or "")
    request_id = envelope.request_id
    with otel_span("webhook.receive", {"event_id": 0}) as _span:
        active_trace_id = get_current_trace_id()
        if active_trace_id:
            set_fallback_trace_id(active_trace_id)
        try:
            set_log_context(request_id=request_id, source=envelope.source or "unknown")
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="processing").inc()
            WEBHOOK_RECEIVED_TOTAL.labels(source=metric_source, status="received").inc()

            async with _pipeline_step(
                step_name="parse",
                metric_source=metric_source,
                span_name="webhook.parse",
                span_attrs={"source": envelope.source or "unknown", "event_id": 0, "pipeline.step": "parse"},
            ) as (parse_span, step_ctx):
                try:
                    req_ctx = parse_request(
                        client_ip,
                        envelope.headers,
                        envelope.payload or {},
                        envelope.raw_body,
                        envelope.source,
                        envelope.event_ts,
                    )
                except Exception as exc:
                    step_ctx.outcome = "error"
                    set_span_error(parse_span, exc)
                    raise

            alert_hash = generate_alert_hash(req_ctx.parsed_data, req_ctx.source)
            set_log_context(alert_hash=alert_hash, source=req_ctx.source or "unknown", request_id=request_id)
            ctx = WebhookProcessContext(
                event_id=None,
                request_id=request_id,
                client_ip=client_ip,
                metric_source=metric_source,
                req_ctx=req_ctx,
                alert_hash=alert_hash,
            )
            logger.info(
                "[Pipeline] 开始处理 request_id=%s source=%s adapter=%s body_size=%d",
                request_id,
                req_ctx.source,
                req_ctx.parsed_data.get("_adapter", req_ctx.source),
                len(envelope.raw_body),
            )
            if _span:
                _span.set_attribute(WEBHOOK_SOURCE, req_ctx.source or "unknown")
                _span.set_attribute(WEBHOOK_ALERT_HASH, alert_hash[:12])

            result = await _ProcessingRun(ctx, dependencies).run()
            if result.suppressed:
                outcome = "suppressed"
                return

            _log_completed_processing(
                ctx=ctx,
                result=result,
                request_id=request_id,
                start_perf=start_perf,
                span=_span,
            )
            outcome = "completed"
            set_log_context(processing_status=WebhookProcessingStatus.COMPLETED.value)
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="completed").inc()
        except Exception as e:
            set_span_error(_span, e)
            outcome = "failed"
            logger.error(
                "[Pipeline] raw webhook processing failed request_id=%s source=%s error=%s",
                request_id,
                metric_source,
                e,
                exc_info=True,
            )
            raise
        finally:
            _record_pipeline_duration(metric_source, outcome, start_perf, _span)
