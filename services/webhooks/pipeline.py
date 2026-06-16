"""Main webhook processing pipeline.

Coordinates raw webhook envelopes through parsing, analysis, noise reduction,
final persistence and forwarding intent creation.
"""

import sys
import time
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from contracts.webhook_payload import WEBHOOK_ADAPTER
from core import json
from core.app_context import get_config_manager
from core.datetime_utils import utc_isoformat, utcnow
from core.log_context import clear_log_context, set_log_context
from core.logger import get_logger
from core.observability.attributes import (
    WEBHOOK_ALERT_HASH,
    WEBHOOK_EVENT_ID,
    WEBHOOK_EVENT_TYPE,
    WEBHOOK_IMPORTANCE,
    WEBHOOK_OUTCOME,
    WEBHOOK_PROCESSING_DURATION_MS,
    WEBHOOK_ROUTE,
    WEBHOOK_SOURCE,
)
from core.observability.metrics import (
    WEBHOOK_PROCESSING_DURATION_SECONDS,
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    sanitize_source,
)
from core.observability.tracing import (
    add_span_event_to,
    generate_trace_id,
    get_current_trace_id,
    otel_span,
    reset_fallback_trace_id,
    set_fallback_trace_id,
    set_span_error,
    set_span_ok,
)
from services.dedup import generate_event_keys
from services.webhooks import pipeline_orchestrator, pipeline_runtime
from services.webhooks.decisioning import normalize_importance
from services.webhooks.repository import EventEnvelope
from services.webhooks.types import (
    WebhookProcessContext,
    WebhookProcessingStatus,
    WebhookRequestContext,
    analysis_route,
)


def parse_request(
    client_ip: str,
    headers: dict[str, Any],
    payload: dict[str, Any],
    raw_body: bytes,
    source: str | None,
    ts: str | None,
) -> WebhookRequestContext:
    from adapters.ecosystem_adapters import normalize_webhook_event

    src = source or headers.get("x-webhook-source", "unknown")
    if not payload and raw_body:
        loaded = json.loads(raw_body)
        payload = loaded if isinstance(loaded, dict) else {}
    norm = normalize_webhook_event(payload, src)
    return WebhookRequestContext(
        client_ip=client_ip,
        source=norm.source,
        payload=raw_body,
        parsed_data=norm.data,
        webhook_full_data={
            "body": payload,
            "headers": headers,
            "parsed_data": dict(norm.data),
            "source": norm.source,
            "timestamp": ts or "",
        },
        headers=headers,
    )


logger = get_logger("webhooks.pipeline")
_PIPELINE_RUNTIME_ERRORS = (OSError, RuntimeError, SQLAlchemyError, ValueError)
_PARSE_ERRORS = (TypeError, ValueError, json.JSONDecodeError)


def _default_pipeline_dependencies() -> pipeline_runtime.WebhookPipelineDependencies:
    config = get_config_manager()
    return pipeline_runtime.WebhookPipelineDependencies(
        dedup_window_seconds=int(config.retry.DEDUP_WINDOW_SECONDS),
    )

# ── Main entry point ─────────────────────────────────────────────────────────


async def handle_webhook_ingest(
    *,
    source: str,
    raw_headers: dict[str, Any],
    raw_body: str,
    client_ip: str = "",
    request_id: str | None = None,
    received_at: str | None = None,
    dependencies: pipeline_runtime.WebhookPipelineDependencies | None = None,
) -> None:
    """Process a newly ingested webhook without pre-writing it to PostgreSQL."""
    request_context_id = request_id or generate_trace_id()
    trace_id = get_current_trace_id() or generate_trace_id()
    trace_token = set_fallback_trace_id(trace_id)
    clear_log_context()
    set_log_context(request_id=request_context_id, webhook_source=source)
    try:
        logger.info(
            "[Pipeline] raw ingest started request_id=%s source=%s ip=%s body_size=%d received_at=%s",
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
            except json.JSONDecodeError:
                payload = None

        env = EventEnvelope(
            headers=dict(raw_headers or {}),
            payload=payload,
            raw_body=raw_body.encode("utf-8"),
            source=source,
            event_ts=received_at or utc_isoformat(utcnow()),
            request_id=request_id,
        )
        await _handle_raw_ingest(
            env,
            client_ip,
            dependencies=dependencies or _default_pipeline_dependencies(),
        )
    finally:
        reset_fallback_trace_id(trace_token)


def _log_completed_processing(
    *,
    ctx: WebhookProcessContext,
    result: pipeline_runtime.PipelineProcessingResult,
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
        "[Pipeline] Alert persisted event_id=%s request_id=%s duplicate=%s original_id=%s",
        save_res.webhook_id,
        request_id,
        save_res.is_duplicate,
        save_res.original_id,
    )

    event_type = "duplicate" if save_res.is_duplicate else "new"
    importance = normalize_importance(final_analysis.get("importance", "unknown"))
    route_label = analysis_route(final_analysis)
    noise_relation = noise.relation
    duration_ms = int((time.perf_counter() - start_perf) * 1000)

    fwd_dec = result.forward_decision
    if fwd_dec is None:
        fwd_info = " forward=unknown"
    elif not fwd_dec.should_forward:
        fwd_info = f" forward=no skip={fwd_dec.skip_reason or 'unknown'}"
    else:
        fwd_info = f" forward=queued rules={len(fwd_dec.matched_rules)} targets={result.outbox_count}"
        if result.outbox_count == 0:
            fwd_info = f" forward=no_target rules={len(fwd_dec.matched_rules)} targets=0"
        if fwd_dec.is_periodic_reminder:
            fwd_info += "(periodic)"

    logger.info(
        "[Pipeline] Processing completed event_id=%s request_id=%s type=%s importance=%s route=%s noise=%s%s duration=%dms",
        save_res.webhook_id,
        request_id,
        event_type,
        importance,
        route_label,
        noise_relation,
        fwd_info,
        duration_ms,
    )

    if span:
        span.set_attribute(WEBHOOK_IMPORTANCE, importance)
        span.set_attribute(WEBHOOK_ROUTE, route_label)
        span.set_attribute(WEBHOOK_EVENT_TYPE, event_type)
        span.set_attribute(WEBHOOK_PROCESSING_DURATION_MS, duration_ms)


async def _handle_raw_ingest(
    envelope: EventEnvelope,
    client_ip: str = "",
    *,
    dependencies: pipeline_runtime.WebhookPipelineDependencies,
) -> None:
    start_perf = time.perf_counter()
    outcome = "unknown"
    metric_source = sanitize_source(envelope.source or "")
    request_id = envelope.request_id
    with otel_span("webhook.receive", {WEBHOOK_EVENT_ID: 0}) as _span:
        try:
            set_log_context(request_id=request_id, webhook_source=envelope.source or "unknown")
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="processing").inc()
            WEBHOOK_RECEIVED_TOTAL.labels(source=metric_source, status="received").inc()

            async with pipeline_runtime.ingest_step(
                step_name="parse",
                metric_source=metric_source,
                source=envelope.source,
            ) as (parse_span, _outcome):
                try:
                    req_ctx = parse_request(
                        client_ip,
                        envelope.headers,
                        envelope.payload or {},
                        envelope.raw_body,
                        envelope.source,
                        envelope.event_ts,
                    )
                except _PARSE_ERRORS as exc:
                    set_span_error(parse_span, exc)
                    raise

            alert_hash, dedup_key = generate_event_keys(req_ctx.parsed_data, req_ctx.source)
            set_log_context(alert_hash=alert_hash, webhook_source=req_ctx.source or "unknown", request_id=request_id)
            ctx = WebhookProcessContext(
                event_id=None,
                request_id=request_id,
                metric_source=metric_source,
                req_ctx=req_ctx,
                alert_hash=alert_hash,
                dedup_key=dedup_key,
            )
            logger.info(
                "[Pipeline] Started processing request_id=%s source=%s adapter=%s body_size=%d",
                request_id,
                req_ctx.source,
                req_ctx.parsed_data.get(WEBHOOK_ADAPTER, req_ctx.source),
                len(envelope.raw_body),
            )
            if _span:
                _span.set_attribute(WEBHOOK_SOURCE, req_ctx.source or "unknown")
                _span.set_attribute(WEBHOOK_ALERT_HASH, alert_hash[:12])

            result = await pipeline_orchestrator.run_processing_pipeline(ctx, dependencies)
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
            set_log_context(webhook_status=WebhookProcessingStatus.COMPLETED.value)
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="completed").inc()
        except _PIPELINE_RUNTIME_ERRORS as e:
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
            if sys.exception() is not None:
                outcome = "failed"
            duration = time.perf_counter() - start_perf
            if _span:
                _span.set_attribute(WEBHOOK_OUTCOME, outcome)
                _span.set_attribute(WEBHOOK_PROCESSING_DURATION_MS, int(duration * 1000))
                add_span_event_to(
                    _span,
                    "webhook.receive.completed",
                    {
                        WEBHOOK_OUTCOME: outcome,
                        WEBHOOK_PROCESSING_DURATION_MS: int(duration * 1000),
                    },
                )
                if outcome != "failed":
                    set_span_ok(_span)
            WEBHOOK_PROCESSING_DURATION_SECONDS.labels(source=metric_source, outcome=outcome).observe(duration)
