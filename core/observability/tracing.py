"""OpenTelemetry tracing setup and lightweight span helpers."""

from __future__ import annotations

import contextvars
import logging
import os
import secrets
import uuid
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager, suppress
from typing import Any, Protocol, cast

from core.observability.attributes import INSTRUMENTATION_SCOPE_NAME, normalize_attributes
from core.observability.exporters import build_span_exporter, env_flag, otel_enabled
from core.observability.resource import build_resource, get_otel_schema_url, get_service_version

_httpx_instrumented = False
_redis_instrumented = False
_sqlalchemy_instrumented = False
_fastapi_instrumented = False
_provider_initialized = False
_trace_provider: Any | None = None
_fallback_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("fallback_trace_id", default="")


class SpanLike(Protocol):
    def set_attribute(self, key: str, value: object) -> None: ...

    def set_status(self, status: object, description: str | None = None) -> None: ...


def instrument_httpx() -> None:
    global _httpx_instrumented
    if _httpx_instrumented or not otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        return
    HTTPXClientInstrumentor().instrument()
    _httpx_instrumented = True


def instrument_redis() -> None:
    global _redis_instrumented
    if _redis_instrumented or not otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except ImportError:
        return
    RedisInstrumentor().instrument()
    _redis_instrumented = True


def instrument_sqlalchemy(engine: Any) -> None:
    global _sqlalchemy_instrumented
    if _sqlalchemy_instrumented or not otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        return
    SQLAlchemyInstrumentor().instrument(engine=engine)
    _sqlalchemy_instrumented = True


def setup_tracing(app: Any | None = None, *, service_name: str | None = None) -> None:
    global _fastapi_instrumented, _provider_initialized, _trace_provider
    if not otel_enabled():
        return

    if not _provider_initialized:
        try:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
            from opentelemetry.trace import set_tracer_provider
        except ImportError:
            return

        provider = TracerProvider(resource=build_resource(service_name), sampler=_build_sampler())
        set_tracer_provider(provider)
        _trace_provider = provider

        has_exporter = False
        if env_flag("OTEL_CONSOLE_EXPORTER", default=False):
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            has_exporter = True

        exporter = build_span_exporter()
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))
            has_exporter = True

        if not has_exporter:
            logging.getLogger("webhook_service").warning("[OTEL] tracing enabled but no trace exporter is configured")
        _provider_initialized = True

    if app is not None and not _fastapi_instrumented:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        except ImportError:
            return
        exclude = "/live,/ready,/static"
        excluded_urls = (env_flag("OTEL_INCLUDE_HEALTHCHECKS", default=False) and "/static") or exclude
        excluded_urls = os.getenv("OTEL_EXCLUDED_URLS", excluded_urls).strip()
        FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded_urls)
        _fastapi_instrumented = True

    instrument_httpx()
    instrument_redis()


def shutdown_tracing() -> None:
    global _provider_initialized, _trace_provider
    provider = _trace_provider
    if provider is None:
        return
    with suppress(Exception):
        provider.force_flush()
    with suppress(Exception):
        provider.shutdown()
    _trace_provider = None
    _provider_initialized = False


def _build_sampler() -> Any:
    try:
        from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased
    except ImportError:
        return None

    sampler_name = os.getenv("OTEL_TRACES_SAMPLER", "parentbased_always_on").strip().lower()
    if sampler_name in {"always_on", "alwayson"}:
        return ALWAYS_ON
    if sampler_name in {"always_off", "alwaysoff"}:
        return ALWAYS_OFF
    if sampler_name == "traceidratio":
        return TraceIdRatioBased(_trace_sample_ratio(default=1.0))
    if sampler_name == "parentbased_traceidratio":
        return ParentBased(TraceIdRatioBased(_trace_sample_ratio(default=1.0)))
    if sampler_name == "parentbased_always_off":
        return ParentBased(ALWAYS_OFF)
    if sampler_name == "parentbased_always_on":
        return ParentBased(ALWAYS_ON)

    logging.getLogger("webhook_service").warning(
        "[OTEL] unknown trace sampler %s; using parentbased_always_on", sampler_name
    )
    return ParentBased(ALWAYS_ON)


def _trace_sample_ratio(*, default: float) -> float:
    raw = os.getenv("OTEL_TRACES_SAMPLER_ARG", str(default)).strip()
    try:
        ratio = float(raw)
    except ValueError:
        return default
    return max(0.0, min(1.0, ratio))


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[SpanLike | None]:
    if not otel_enabled():
        yield None
        return
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    tracer = trace.get_tracer(
        INSTRUMENTATION_SCOPE_NAME,
        instrumenting_library_version=get_service_version(),
        schema_url=get_otel_schema_url(),
    )
    with tracer.start_as_current_span(name) as current:
        span_obj = cast(SpanLike, current) if current is not None else None
        if span_obj is not None:
            for key, value in normalize_attributes(attributes).items():
                with suppress((AttributeError, RuntimeError, TypeError, ValueError)):
                    span_obj.set_attribute(key, value)
        try:
            yield span_obj
        except Exception as exc:
            set_span_error(span_obj, exc)
            raise


def get_otel_trace_id() -> str:
    if not otel_enabled():
        return ""
    with suppress((AttributeError, RuntimeError, TypeError, ValueError)):
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    return ""


def get_otel_trace_flags() -> str:
    if not otel_enabled():
        return "00"
    with suppress((AttributeError, RuntimeError, TypeError, ValueError)):
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(int(ctx.trace_flags), "02x")
    return "00"


def _normalize_trace_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if len(lowered) == 32 and all(c in "0123456789abcdef" for c in lowered):
        return lowered
    return ""


def generate_trace_id() -> str:
    return uuid.uuid4().hex


def set_fallback_trace_id(trace_id: str) -> contextvars.Token[str]:
    return _fallback_trace_id_var.set(_normalize_trace_id(trace_id) or generate_trace_id())


def reset_fallback_trace_id(token: contextvars.Token[str]) -> None:
    _fallback_trace_id_var.reset(token)


def get_fallback_trace_id() -> str:
    return _fallback_trace_id_var.get()


def get_current_trace_id() -> str:
    return get_otel_trace_id() or get_fallback_trace_id()


def build_traceparent(trace_id: str) -> str:
    trace_id_hex = _normalize_trace_id(trace_id) or uuid.uuid4().hex
    span_id = secrets.token_hex(8)
    return f"00-{trace_id_hex}-{span_id}-01"


def _header_value(headers: Mapping[str, Any], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value or "").strip()
    return ""


def _has_header(headers: Mapping[str, Any], name: str) -> bool:
    wanted = name.lower()
    return any(str(key).lower() == wanted for key in headers)


def extract_trace_id_from_headers(headers: Mapping[str, Any]) -> str:
    traceparent = _header_value(headers, "traceparent")
    if not traceparent:
        return ""
    parts = traceparent.split("-")
    if len(parts) != 4:
        return ""
    trace_id = parts[1]
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id.lower()):
        return ""
    return trace_id.lower()


def extract_request_id_from_headers(headers: Mapping[str, Any]) -> str:
    return _header_value(headers, "x-request-id")


def inject_trace_headers(
    carrier: MutableMapping[str, str],
    *,
    request_id: str | None = None,
    fallback_trace_id: str | None = None,
) -> MutableMapping[str, str]:
    """Inject W3C trace context and a separate business request id."""
    if otel_enabled():
        with suppress(Exception):
            from opentelemetry import propagate

            propagate.inject(carrier)

    current_trace_id = get_current_trace_id()
    if not _has_header(carrier, "traceparent"):
        trace_id = current_trace_id or (fallback_trace_id or "")
        if trace_id:
            carrier["traceparent"] = build_traceparent(trace_id)

    if not _has_header(carrier, "X-Request-Id"):
        request_value = (request_id or "").strip() or current_trace_id or (fallback_trace_id or "")
        if request_value:
            carrier["X-Request-Id"] = request_value

    return carrier


@contextmanager
def trace_context_from_headers(headers: Mapping[str, Any] | None) -> Iterator[None]:
    """Attach an incoming W3C trace context for worker-side child spans."""
    headers = headers or {}
    traceparent = str(headers.get("traceparent") or headers.get("Traceparent") or "").strip()
    if not otel_enabled() or not traceparent:
        yield
        return
    try:
        from opentelemetry import context, propagate
    except ImportError:
        yield
        return

    extracted = propagate.extract(headers)
    token = context.attach(extracted)
    try:
        yield
    finally:
        with suppress(Exception):
            context.detach(token)


def get_otel_span_id() -> str:
    if not otel_enabled():
        return ""
    with suppress(Exception):
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.span_id, "016x")
    return ""


def set_span_error(span_obj: Any | None, error: BaseException | str) -> None:
    """Mark a span as failed and attach the exception when possible."""
    if span_obj is None:
        return
    with suppress(Exception):
        from opentelemetry.trace import StatusCode

        description = str(error)
        span_obj.set_status(StatusCode.ERROR, description)
        if isinstance(error, BaseException) and hasattr(span_obj, "record_exception"):
            span_obj.record_exception(error)


def set_span_ok(span_obj: Any | None) -> None:
    """Mark a span as successfully completed when the SDK supports it."""
    if span_obj is None:
        return
    with suppress(Exception):
        from opentelemetry.trace import StatusCode

        span_obj.set_status(StatusCode.OK)


def add_span_event_to(span_obj: Any | None, name: str, attributes: Mapping[str, Any] | None = None) -> None:
    if span_obj is None or not hasattr(span_obj, "add_event"):
        return
    with suppress(Exception):
        span_obj.add_event(name, normalize_attributes(attributes))


def set_current_span_error(error: BaseException | str) -> None:
    if not otel_enabled():
        return
    with suppress(Exception):
        from opentelemetry import trace

        current = trace.get_current_span()
        context = current.get_span_context()
        if context and context.is_valid:
            set_span_error(current, error)


otel_span = span
