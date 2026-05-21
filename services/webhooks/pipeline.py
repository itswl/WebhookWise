"""Webhook 处理主管线。

Coordinates raw webhook envelopes through parsing, analysis, noise reduction,
final persistence and forwarding intent creation.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.log_context import clear_log_context, set_log_context
from core.logger import logger
from core.observability.attributes import (
    WEBHOOK_ALERT_HASH,
    WEBHOOK_EVENT_TYPE,
    WEBHOOK_IMPORTANCE,
    WEBHOOK_OUTCOME,
    WEBHOOK_PROCESSING_DURATION_MS,
    WEBHOOK_ROUTE,
    WEBHOOK_SOURCE,
)
from core.observability.metrics import (
    WEBHOOK_NOISE_REDUCED_TOTAL,
    WEBHOOK_PIPELINE_STEP_DURATION_SECONDS,
    WEBHOOK_PIPELINE_STEP_TOTAL,
    WEBHOOK_PROCESSING_DURATION_SECONDS,
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    sanitize_source,
)
from core.observability.tracing import (
    generate_trace_id,
    get_current_trace_id,
    set_fallback_trace_id,
    set_span_error,
)
from core.observability.tracing import (
    span as otel_span,
)
from models import WebhookEvent
from services.webhooks.decisioning import normalize_importance
from services.webhooks.pipeline_steps import (
    PipelineProcessingResult,
    WebhookPipelineDependencies,
    run_processing_steps,
)
from services.webhooks.repository import EventEnvelope
from services.webhooks.request_parser import parse_request
from services.webhooks.types import (
    WebhookProcessContext,
    WebhookProcessingStatus,
)

# ── 主入口 ───────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _PipelineExecutionState:
    start_perf: float
    outcome: str = "unknown"
    metric_source: str = "unknown"
    request_id: str | None = None


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
    env = EventEnvelope(
        headers=dict(raw_headers or {}),
        payload=None,
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


def _record_pipeline_duration(state: _PipelineExecutionState, span: Any | None) -> None:
    duration = time.perf_counter() - state.start_perf
    if span:
        span.set_attribute(WEBHOOK_OUTCOME, state.outcome)
    WEBHOOK_PROCESSING_DURATION_SECONDS.labels(source=state.metric_source, outcome=state.outcome).observe(duration)


async def _handle_raw_ingest(
    envelope: EventEnvelope,
    client_ip: str = "",
    *,
    dependencies: WebhookPipelineDependencies,
) -> None:
    state = _PipelineExecutionState(start_perf=time.perf_counter())
    with otel_span("webhook.receive", {"event_id": 0}) as _span:
        active_trace_id = get_current_trace_id()
        if active_trace_id:
            set_fallback_trace_id(active_trace_id)
        try:
            await _process_envelope(client_ip, envelope, dependencies, state, _span)
        except Exception as e:
            set_span_error(_span, e)
            state.outcome = "failed"
            logger.error(
                "[Pipeline] raw webhook processing failed request_id=%s source=%s error=%s",
                state.request_id,
                state.metric_source,
                e,
                exc_info=True,
            )
            raise
        finally:
            _record_pipeline_duration(state, _span)


async def _process_envelope(
    client_ip: str,
    env: EventEnvelope,
    dependencies: WebhookPipelineDependencies,
    state: _PipelineExecutionState,
    span: Any | None,
) -> None:
    state.metric_source = sanitize_source(env.source or "")
    state.request_id = env.request_id
    set_log_context(request_id=state.request_id, source=env.source or "unknown")
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="processing").inc()
    WEBHOOK_RECEIVED_TOTAL.labels(source=state.metric_source, status="received").inc()

    parse_start = time.perf_counter()
    parse_outcome = "success"
    with otel_span(
        "webhook.parse",
        {"source": env.source or "unknown", "event_id": 0, "pipeline.step": "parse"},
    ) as parse_span:
        try:
            req_ctx = parse_request(client_ip, env.headers, env.payload or {}, env.raw_body, env.source, env.event_ts)
        except Exception as exc:
            parse_outcome = "error"
            set_span_error(parse_span, exc)
            raise
        finally:
            WEBHOOK_PIPELINE_STEP_TOTAL.labels("parse", state.metric_source, parse_outcome).inc()
            WEBHOOK_PIPELINE_STEP_DURATION_SECONDS.labels("parse", state.metric_source, parse_outcome).observe(
                time.perf_counter() - parse_start
            )
    alert_hash = WebhookEvent.generate_hash(req_ctx.parsed_data, req_ctx.source)
    set_log_context(alert_hash=alert_hash, source=req_ctx.source or "unknown", request_id=state.request_id)
    ctx = WebhookProcessContext(
        event_id=None,
        request_id=state.request_id,
        client_ip=client_ip,
        metric_source=state.metric_source,
        req_ctx=req_ctx,
        alert_hash=alert_hash,
    )
    logger.info(
        "[Pipeline] 开始处理 request_id=%s source=%s adapter=%s body_size=%d",
        state.request_id,
        req_ctx.source,
        req_ctx.parsed_data.get("_adapter", req_ctx.source),
        len(env.raw_body),
    )
    if span:
        span.set_attribute(WEBHOOK_SOURCE, req_ctx.source or "unknown")
        span.set_attribute(WEBHOOK_ALERT_HASH, alert_hash[:12])

    result = await run_processing_steps(ctx, dependencies)
    if result.suppressed:
        state.outcome = "suppressed"
        return
    _log_completed_processing(
        ctx=ctx,
        result=result,
        request_id=state.request_id,
        start_perf=state.start_perf,
        span=span,
    )

    state.outcome = "completed"
    set_log_context(processing_status=WebhookProcessingStatus.COMPLETED.value)
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="completed").inc()
