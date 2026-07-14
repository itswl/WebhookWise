from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _install_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: object) -> ModuleType:
    module = ModuleType(name)
    module.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, module)
    if "." in name:
        parent_name, attr_name = name.rsplit(".", 1)
        try:
            parent = importlib.import_module(parent_name)
        except ImportError:
            parent = _install_module(monkeypatch, parent_name)
            parent.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setattr(parent, attr_name, module, raising=False)
    return module


@pytest.fixture
def tracing_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    from core.observability import tracing

    monkeypatch.setattr(tracing, "_httpx_instrumented", False)
    monkeypatch.setattr(tracing, "_redis_instrumented", False)
    monkeypatch.setattr(tracing, "_sqlalchemy_instrumented", False)
    monkeypatch.setattr(tracing, "_fastapi_instrumented", False)
    monkeypatch.setattr(tracing, "_provider_initialized", False)
    monkeypatch.setattr(tracing, "_trace_provider", None)
    return tracing


@pytest.fixture
def metrics_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    from core.observability import metrics_base

    monkeypatch.setattr(metrics_base, "_provider_initialized", False)
    monkeypatch.setattr(metrics_base, "_meter_provider", None)
    return metrics_base


def test_trace_instrumentors_are_best_effort_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tracing_state: Any,
) -> None:
    tracing = tracing_state
    calls: list[tuple[str, object | None]] = []

    class HTTPXClientInstrumentor:
        def instrument(self) -> None:
            calls.append(("httpx", None))

    class RedisInstrumentor:
        def instrument(self) -> None:
            calls.append(("redis", None))

    class SQLAlchemyInstrumentor:
        def instrument(self, *, engine: object) -> None:
            calls.append(("sqlalchemy", engine))

    _install_module(monkeypatch, "opentelemetry.instrumentation.httpx", HTTPXClientInstrumentor=HTTPXClientInstrumentor)
    _install_module(monkeypatch, "opentelemetry.instrumentation.redis", RedisInstrumentor=RedisInstrumentor)
    _install_module(
        monkeypatch,
        "opentelemetry.instrumentation.sqlalchemy",
        SQLAlchemyInstrumentor=SQLAlchemyInstrumentor,
    )
    monkeypatch.setattr(tracing, "otel_enabled", lambda: True)

    engine = object()
    tracing.instrument_httpx()
    tracing.instrument_httpx()
    tracing.instrument_redis()
    tracing.instrument_redis()
    tracing.instrument_sqlalchemy(engine)
    tracing.instrument_sqlalchemy(object())

    assert calls == [("httpx", None), ("redis", None), ("sqlalchemy", engine)]


def test_trace_sampler_parses_supported_modes_and_clamps_ratios(
    monkeypatch: pytest.MonkeyPatch,
    tracing_state: Any,
) -> None:
    tracing = tracing_state

    class TraceIdRatioBased:
        def __init__(self, ratio: float) -> None:
            self.ratio = ratio

    class ParentBased:
        def __init__(self, root: object) -> None:
            self.root = root

    _install_module(
        monkeypatch,
        "opentelemetry.sdk.trace.sampling",
        ALWAYS_ON="always-on",
        ALWAYS_OFF="always-off",
        TraceIdRatioBased=TraceIdRatioBased,
        ParentBased=ParentBased,
    )

    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    assert tracing._build_sampler() == "always-off"

    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "2.5")
    sampler = tracing._build_sampler()
    assert isinstance(sampler, TraceIdRatioBased)
    assert sampler.ratio == 1.0

    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "bad")
    parent_sampler = tracing._build_sampler()
    assert isinstance(parent_sampler, ParentBased)
    assert isinstance(parent_sampler.root, TraceIdRatioBased)
    assert parent_sampler.root.ratio == 1.0

    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "unexpected")
    fallback = tracing._build_sampler()
    assert isinstance(fallback, ParentBased)
    assert fallback.root == "always-on"


def test_setup_and_shutdown_tracing_wires_provider_exporters_and_fastapi(
    monkeypatch: pytest.MonkeyPatch,
    tracing_state: Any,
) -> None:
    tracing = tracing_state
    processors: list[object] = []
    providers: list[Any] = []
    apps: list[tuple[object, str]] = []
    instrumented: list[str] = []

    class TracerProvider:
        def __init__(self, *, resource: object, sampler: object) -> None:
            self.resource = resource
            self.sampler = sampler
            self.flushed = False
            self.closed = False

        def add_span_processor(self, processor: object) -> None:
            processors.append(processor)

        def force_flush(self) -> None:
            self.flushed = True

        def shutdown(self) -> None:
            self.closed = True

    class BatchSpanProcessor:
        def __init__(self, exporter: object) -> None:
            self.exporter = exporter

    class ConsoleSpanExporter:
        pass

    class FastAPIInstrumentor:
        @staticmethod
        def instrument_app(app: object, *, excluded_urls: str) -> None:
            apps.append((app, excluded_urls))

    def set_tracer_provider(provider: object) -> None:
        providers.append(provider)

    _install_module(monkeypatch, "opentelemetry.sdk.trace", TracerProvider=TracerProvider)
    _install_module(
        monkeypatch,
        "opentelemetry.sdk.trace.export",
        BatchSpanProcessor=BatchSpanProcessor,
        ConsoleSpanExporter=ConsoleSpanExporter,
    )
    _install_module(monkeypatch, "opentelemetry.trace", set_tracer_provider=set_tracer_provider)
    _install_module(monkeypatch, "opentelemetry.instrumentation.fastapi", FastAPIInstrumentor=FastAPIInstrumentor)
    monkeypatch.setattr(tracing, "otel_enabled", lambda: True)
    monkeypatch.setattr(tracing, "build_resource", lambda service_name=None: {"service.name": service_name})
    monkeypatch.setattr(tracing, "_build_sampler", lambda: "sample")
    monkeypatch.setattr(tracing, "build_span_exporter", lambda: "otlp-exporter")
    monkeypatch.setattr(
        tracing,
        "env_flag",
        lambda name, default=False: name in {"OTEL_CONSOLE_EXPORTER", "OTEL_TRACES_ENABLED"},
    )
    monkeypatch.setattr(tracing, "instrument_httpx", lambda: instrumented.append("httpx"))
    monkeypatch.setattr(tracing, "instrument_redis", lambda: instrumented.append("redis"))

    app = object()
    tracing.setup_tracing(app, service_name="webhookwise-api")
    provider = providers[0]

    assert provider.resource == {"service.name": "webhookwise-api"}
    assert provider.sampler == "sample"
    assert [type(processor.exporter).__name__ for processor in processors[:1]] == ["ConsoleSpanExporter"]
    assert processors[1].exporter == "otlp-exporter"
    assert apps == [(app, "/live,/ready,/static")]
    assert instrumented == ["httpx", "redis"]

    tracing.shutdown_tracing()
    assert provider.flushed is True
    assert provider.closed is True
    assert tracing._trace_provider is None


def test_span_context_sets_attributes_and_marks_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    tracing_state: Any,
) -> None:
    tracing = tracing_state
    spans: list[Any] = []

    class Span:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}
            self.statuses: list[tuple[object, str | None]] = []
            self.exceptions: list[BaseException] = []

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

        def set_status(self, status: object, description: str | None = None) -> None:
            self.statuses.append((status, description))

        def record_exception(self, exc: BaseException) -> None:
            self.exceptions.append(exc)

    class StartedSpan:
        def __enter__(self) -> Span:
            span = Span()
            spans.append(span)
            return span

        def __exit__(self, *_exc: object) -> bool:
            return False

    class Tracer:
        def start_as_current_span(self, name: str) -> StartedSpan:
            assert name in {"ok", "boom"}
            return StartedSpan()

    trace_module = _install_module(
        monkeypatch,
        "opentelemetry.trace",
        get_tracer=lambda *_args, **_kwargs: Tracer(),
        StatusCode=SimpleNamespace(ERROR="ERROR"),
    )
    monkeypatch.setattr(tracing, "otel_enabled", lambda: True)

    with tracing.span("ok", {"webhook.source": "prod", "ignored": None}) as current:
        assert current is spans[-1]
    assert spans[-1].attributes["webhook.source"] == "prod"
    assert "ignored" not in spans[-1].attributes

    with pytest.raises(RuntimeError, match="explode"), tracing.span("boom"):
        raise RuntimeError("explode")
    assert spans[-1].statuses == [("ERROR", "explode")]
    assert isinstance(spans[-1].exceptions[0], RuntimeError)
    assert sys.modules["opentelemetry.trace"] is trace_module


def test_trace_header_injection_extraction_and_context_attach(
    monkeypatch: pytest.MonkeyPatch,
    tracing_state: Any,
) -> None:
    tracing = tracing_state
    actions: list[tuple[str, object]] = []

    class Propagate:
        @staticmethod
        def inject(carrier: dict[str, str]) -> None:
            carrier["x-injected"] = "1"

        @staticmethod
        def extract(headers: object) -> object:
            actions.append(("extract", headers))
            return {"ctx": headers}

    class Context:
        @staticmethod
        def attach(extracted: object) -> str:
            actions.append(("attach", extracted))
            return "token"

        @staticmethod
        def detach(token: object) -> None:
            actions.append(("detach", token))

    _install_module(monkeypatch, "opentelemetry.propagate", inject=Propagate.inject, extract=Propagate.extract)
    _install_module(monkeypatch, "opentelemetry.context", attach=Context.attach, detach=Context.detach)
    monkeypatch.setattr(tracing, "otel_enabled", lambda: True)
    monkeypatch.setattr(tracing, "get_current_trace_id", lambda: "a" * 32)

    carrier: dict[str, str] = {}
    tracing.inject_trace_headers(carrier, request_id="req-1")

    assert carrier["x-injected"] == "1"
    assert carrier["traceparent"].startswith(f"00-{'a' * 32}-")
    assert carrier["X-Request-Id"] == "req-1"
    assert tracing.extract_trace_id_from_headers({"Traceparent": carrier["traceparent"]}) == "a" * 32
    assert tracing.extract_request_id_from_headers({"x-request-id": " req-2 "}) == "req-2"

    with tracing.trace_context_from_headers({"traceparent": carrier["traceparent"]}):
        actions.append(("inside", True))

    assert actions[0][0] == "extract"
    assert actions[-2:] == [("inside", True), ("detach", "token")]


def test_current_otel_ids_read_valid_span_context(monkeypatch: pytest.MonkeyPatch, tracing_state: Any) -> None:
    tracing = tracing_state

    class SpanContext:
        is_valid = True
        trace_id = int("b" * 32, 16)
        span_id = int("c" * 16, 16)
        trace_flags = 1

    class CurrentSpan:
        def get_span_context(self) -> SpanContext:
            return SpanContext()

    _install_module(monkeypatch, "opentelemetry.trace", get_current_span=lambda: CurrentSpan())
    monkeypatch.setattr(tracing, "otel_enabled", lambda: True)

    assert tracing.get_otel_trace_id() == "b" * 32
    assert tracing.get_otel_span_id() == "c" * 16
    assert tracing.get_otel_trace_flags() == "01"


def test_histogram_views_and_meter_provider_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    metrics_state: Any,
) -> None:
    metrics_base = metrics_state
    configured: dict[str, object] = {}

    class View:
        def __init__(self, *, instrument_name: str, aggregation: object) -> None:
            self.instrument_name = instrument_name
            self.aggregation = aggregation

    class ExplicitBucketHistogramAggregation:
        def __init__(self, *, boundaries: object) -> None:
            self.boundaries = boundaries

    class PeriodicExportingMetricReader:
        def __init__(
            self,
            exporter: object,
            *,
            export_interval_millis: int,
            export_timeout_millis: int,
        ) -> None:
            configured["reader"] = (exporter, export_interval_millis, export_timeout_millis)

    class MeterProvider:
        def __init__(self, *, resource: object, metric_readers: list[object], views: list[object]) -> None:
            self.resource = resource
            self.metric_readers = metric_readers
            self.views = views
            self.force_flushed = False
            self.shutdown_called = False

        def force_flush(self) -> None:
            self.force_flushed = True

        def shutdown(self) -> None:
            self.shutdown_called = True

    _install_module(
        monkeypatch,
        "opentelemetry.sdk.metrics.view",
        View=View,
        ExplicitBucketHistogramAggregation=ExplicitBucketHistogramAggregation,
    )
    _install_module(monkeypatch, "opentelemetry.sdk.metrics", MeterProvider=MeterProvider)
    _install_module(
        monkeypatch, "opentelemetry.sdk.metrics.export", PeriodicExportingMetricReader=PeriodicExportingMetricReader
    )
    _install_module(
        monkeypatch, "opentelemetry.metrics", set_meter_provider=lambda provider: configured.update(provider=provider)
    )
    monkeypatch.setattr(metrics_base, "otel_enabled", lambda: True)
    monkeypatch.setattr(metrics_base, "build_metric_exporter", lambda: "metric-exporter")
    monkeypatch.setattr(
        metrics_base, "env_int", lambda name, default: {"OTEL_METRIC_EXPORT_INTERVAL": 10}.get(name, default)
    )
    monkeypatch.setattr(metrics_base, "build_resource", lambda service_name=None: {"service.name": service_name})

    views = metrics_base._histogram_views()
    assert views
    assert views[0].instrument_name == "http.server.request.duration"
    assert views[0].aggregation.boundaries

    metrics_base.setup_meter_provider(service_name="webhookwise-worker")
    provider = configured["provider"]
    assert provider.resource == {"service.name": "webhookwise-worker"}
    assert configured["reader"] == ("metric-exporter", 1000, 30000)

    metrics_base.shutdown_meter_provider()
    assert provider.force_flushed is True
    assert provider.shutdown_called is True
    assert metrics_base._meter_provider is None


def test_metric_wrappers_create_instruments_cache_and_observe_values(
    monkeypatch: pytest.MonkeyPatch,
    metrics_state: Any,
) -> None:
    metrics_base = metrics_state
    calls: list[tuple[str, object, dict[str, object]]] = []

    class Instrument:
        def add(self, amount: object, *, attributes: dict[str, object]) -> None:
            calls.append(("add", amount, attributes))

        def record(self, value: object, *, attributes: dict[str, object], **kwargs: object) -> None:
            calls.append(("record", value, {**attributes, **kwargs}))

    class Meter:
        def __init__(self) -> None:
            self.counter_creates = 0
            self.histogram_creates = 0

        def create_counter(self, *_args: object, **_kwargs: object) -> Instrument:
            self.counter_creates += 1
            return Instrument()

        def create_histogram(self, *_args: object, **_kwargs: object) -> Instrument:
            self.histogram_creates += 1
            return Instrument()

    meter = Meter()
    monkeypatch.setattr(metrics_base, "_get_meter", lambda: meter)

    counter = metrics_base.Counter("jobs.total", "jobs", ("worker.task.name",))
    counter.inc(0)
    counter.labels(name="webhook").inc(2)
    counter.labels("forward").inc(3)
    counter.dec()
    counter.set(1)
    counter.observe(1)

    histogram = metrics_base.Histogram("jobs.duration", "duration", ("worker.task.name",))
    histogram.labels("webhook").observe(1.5)
    histogram.inc(2, {"worker.task.name": "forward"})
    histogram.dec()
    histogram.set(4, {"worker.task.name": "scheduled"})

    assert meter.counter_creates == 1
    assert meter.histogram_creates == 1
    assert calls[0] == ("add", 2, {"worker.task.name": "webhook"})
    assert calls[1] == ("add", 3, {"worker.task.name": "forward"})
    assert any(call[0] == "record" and call[1] == 1.5 and call[2]["worker.task.name"] == "webhook" for call in calls)
    assert any(call[0] == "record" and call[1] == 4 for call in calls)


def test_gauge_stores_series_limits_callbacks_and_observations(
    monkeypatch: pytest.MonkeyPatch,
    metrics_state: Any,
) -> None:
    metrics_base = metrics_state
    observable_callbacks: list[object] = []

    class Observation:
        def __init__(self, value: object, attributes: dict[str, object]) -> None:
            self.value = value
            self.attributes = attributes

    class Meter:
        def create_observable_gauge(self, *_args: object, callbacks: list[object], **_kwargs: object) -> object:
            observable_callbacks.extend(callbacks)
            return object()

    meter = Meter()
    _install_module(monkeypatch, "opentelemetry.metrics", Observation=Observation)
    monkeypatch.setattr(metrics_base, "_get_meter", lambda: meter)
    monkeypatch.setattr(metrics_base, "_MAX_GAUGE_SERIES", 1)

    gauge = metrics_base.Gauge("queue.depth", "queue depth", ("queue.name",))
    gauge.labels("primary").set(10)
    gauge.labels("secondary").set(20)
    gauge.labels("primary").inc(2)
    gauge.labels("primary").dec(5)
    gauge.set_callback(lambda: 7, {"queue.name": "callback"})
    gauge.set_callback(lambda: None, {"queue.name": "none"})
    gauge.set_callback(lambda: (_ for _ in ()).throw(RuntimeError("callback failed")), {"queue.name": "broken"})

    observations = gauge._observe(object())
    observed = {(item.value, item.attributes.get("queue.name")) for item in observations}

    assert observable_callbacks == [gauge._observe]
    assert (7, "primary") in observed
    assert (7, "callback") in observed
    assert all(item.attributes.get("queue.name") != "secondary" for item in observations)


def test_meter_provider_without_exporter_initializes_as_warned_noop(
    monkeypatch: pytest.MonkeyPatch,
    metrics_state: Any,
) -> None:
    metrics_base = metrics_state
    monkeypatch.setattr(metrics_base, "otel_enabled", lambda: True)
    monkeypatch.setattr(metrics_base, "build_metric_exporter", lambda: None)

    metrics_base.setup_meter_provider(service_name="webhookwise-api")

    assert metrics_base._provider_initialized is True
    assert metrics_base._meter_provider is None
