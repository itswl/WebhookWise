"""Shared OpenTelemetry metric primitives."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from core.observability.attributes import normalize_attribute_key, normalize_attribute_value
from core.observability.exporters import build_metric_exporter, otel_enabled
from core.observability.resource import build_resource

_provider_initialized = False
_meter_provider: Any | None = None
_meter_provider_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


_MAX_GAUGE_SERIES = _env_int("WEBHOOKWISE_GAUGE_SERIES_LIMIT", 512)


def _histogram_views() -> list[Any]:
    try:
        from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    except ImportError:
        return []

    fast_seconds = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5]
    request_seconds = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30]
    ai_seconds = [0.5, 1, 2, 5, 10, 20, 30, 60, 120]
    bytes_buckets = [
        512.0,
        1024.0,
        2048.0,
        4096.0,
        8192.0,
        16384.0,
        32768.0,
        65536.0,
        131072.0,
        262144.0,
        524288.0,
        1048576.0,
    ]
    default_seconds = [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60]

    buckets_by_instrument: dict[str, Sequence[float]] = {
        "http.server.request.duration": request_seconds,
        "webhook.processing.duration": request_seconds,
        "webhook.pipeline.step.duration": request_seconds,
        "webhook.noise.evaluation.duration": fast_seconds,
        "webhook.ingress.payload.size": bytes_buckets,
        "ai.request.duration": ai_seconds,
        "ai.cache.operation.duration": fast_seconds,
        "db.session.duration": fast_seconds,
        "redis.operation.duration": fast_seconds,
        "queue.operation.duration": fast_seconds,
        "worker.task.duration": request_seconds,
        "scheduler.task.duration": request_seconds,
        "forward.delivery.duration": default_seconds,
        "forward.outbox.process.duration": default_seconds,
    }
    return [
        View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(boundaries=buckets))
        for name, buckets in buckets_by_instrument.items()
    ]


def setup_meter_provider(*, service_name: str | None = None) -> None:
    global _meter_provider, _provider_initialized
    if _provider_initialized or not otel_enabled():
        return
    with _meter_provider_lock:
        if _provider_initialized or not otel_enabled():
            return
        exporter = build_metric_exporter()
        if exporter is None:
            logging.getLogger("webhook_service").warning("[OTEL] metrics enabled but no metric exporter is configured")
            _provider_initialized = True
            return
        try:
            from opentelemetry import metrics
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        except ImportError:
            return
        reader = PeriodicExportingMetricReader(exporter)
        provider = MeterProvider(
            resource=build_resource(service_name),
            metric_readers=[reader],
            views=_histogram_views(),
        )
        metrics.set_meter_provider(provider)
        _meter_provider = provider
        _provider_initialized = True


def shutdown_meter_provider() -> None:
    global _meter_provider, _provider_initialized
    provider = _meter_provider
    if provider is None:
        return
    try:
        provider.force_flush()
    except Exception:
        logging.getLogger("webhook_service").debug("[OTEL] metric force_flush failed", exc_info=True)
    try:
        provider.shutdown()
    except Exception:
        logging.getLogger("webhook_service").debug("[OTEL] metric shutdown failed", exc_info=True)
    _meter_provider = None
    _provider_initialized = False


def _get_meter() -> Any | None:
    if not otel_enabled():
        return None
    setup_meter_provider()
    try:
        from opentelemetry import metrics
    except ImportError:
        return None
    return metrics.get_meter("webhookwise")


def _attrs_key(attributes: Mapping[str, str | bool | int | float]) -> tuple[tuple[str, str | bool | int | float], ...]:
    return tuple(sorted(attributes.items()))


def _alias_for_key(key: str, label_keys: tuple[str, ...]) -> str:
    if key in label_keys:
        return key
    suffix_matches = [label for label in label_keys if label.endswith(f".{key}")]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    aliased = normalize_attribute_key(key)
    if aliased in label_keys:
        return aliased
    normalized_suffix_matches = [label for label in label_keys if label.endswith(f".{aliased}")]
    if len(normalized_suffix_matches) == 1:
        return normalized_suffix_matches[0]
    return key


class BoundMetric:
    def __init__(self, metric: _MetricBase, attributes: dict[str, str | bool | int | float]) -> None:
        self._metric = metric
        self._attributes = attributes

    def inc(self, amount: int | float = 1) -> None:
        self._metric.inc(amount, self._attributes)

    def dec(self, amount: int | float = 1) -> None:
        self._metric.dec(amount, self._attributes)

    def set(self, value: int | float) -> None:
        self._metric.set(value, self._attributes)

    def observe(self, value: int | float) -> None:
        self._metric.observe(value, self._attributes)


class _MetricBase:
    def __init__(
        self,
        name: str,
        description: str,
        label_keys: Iterable[str] = (),
        *,
        unit: str = "1",
    ) -> None:
        self.name = name
        self.description = description
        self.label_keys = tuple(label_keys)
        self.unit = unit
        self._instrument: Any | None = None
        self._lock = threading.Lock()

    def labels(self, *values: object, **labels: object) -> BoundMetric:
        attributes: dict[str, str | bool | int | float] = {}
        for key, value in zip(self.label_keys, values, strict=False):
            if value is not None:
                attributes[key] = normalize_attribute_value(value)
        for key, value in labels.items():
            if value is None:
                continue
            attributes[_alias_for_key(key, self.label_keys)] = normalize_attribute_value(value)
        return BoundMetric(self, attributes)

    def _get_or_create(self, factory: Callable[[Any], Any]) -> Any | None:
        if self._instrument is not None:
            return self._instrument
        meter = _get_meter()
        if meter is None:
            return None
        with self._lock:
            if self._instrument is None:
                self._instrument = factory(meter)
            return self._instrument

    def inc(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        raise NotImplementedError

    def dec(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        raise NotImplementedError

    def set(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        raise NotImplementedError

    def observe(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        raise NotImplementedError


class Counter(_MetricBase):
    def inc(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        if amount <= 0:
            return
        instrument = self._get_or_create(
            lambda meter: meter.create_counter(self.name, description=self.description, unit=self.unit)
        )
        if instrument is not None:
            instrument.add(amount, attributes=dict(attributes or {}))

    def dec(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        return

    def set(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        return

    def observe(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        return


class Histogram(_MetricBase):
    def observe(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        instrument = self._get_or_create(
            lambda meter: meter.create_histogram(self.name, description=self.description, unit=self.unit)
        )
        if instrument is not None:
            instrument.record(value, attributes=dict(attributes or {}))

    def inc(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        self.observe(amount, attributes)

    def dec(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        return

    def set(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        self.observe(value, attributes)


class Gauge(_MetricBase):
    def __init__(
        self,
        name: str,
        description: str,
        label_keys: Iterable[str] = (),
        *,
        unit: str = "1",
    ) -> None:
        super().__init__(name, description, label_keys, unit=unit)
        self._values: dict[tuple[tuple[str, str | bool | int | float], ...], int | float] = {}
        self._callbacks: list[
            tuple[Callable[[], int | float | None], tuple[tuple[str, str | bool | int | float], ...]]
        ] = []
        self._series_limit_logged = False
        self._callback_error_logged = False

    def _observe(self, options: object) -> list[Any]:
        try:
            from opentelemetry.metrics import Observation
        except ImportError:
            return []
        with self._lock:
            observations = [Observation(value, dict(attrs)) for attrs, value in self._values.items()]
            callbacks = list(self._callbacks)
        for callback, attrs in callbacks:
            try:
                value = callback()
            except Exception as exc:
                if not self._callback_error_logged:
                    logging.getLogger("webhook_service").warning(
                        "[OTEL] gauge callback failed metric=%s error=%s",
                        self.name,
                        exc,
                    )
                    self._callback_error_logged = True
                continue
            if value is not None:
                observations.append(Observation(value, dict(attrs)))
        return observations

    def _ensure_observable(self) -> None:
        self._get_or_create(
            lambda meter: meter.create_observable_gauge(
                self.name,
                callbacks=[self._observe],
                description=self.description,
                unit=self.unit,
            )
        )

    def _can_store_key(self, key: tuple[tuple[str, str | bool | int | float], ...]) -> bool:
        if key in self._values or len(self._values) < _MAX_GAUGE_SERIES:
            return True
        if not self._series_limit_logged:
            logging.getLogger("webhook_service").warning(
                "[OTEL] gauge series limit reached metric=%s limit=%s",
                self.name,
                _MAX_GAUGE_SERIES,
            )
            self._series_limit_logged = True
        return False

    def set(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        key = _attrs_key(attributes or {})
        with self._lock:
            if not self._can_store_key(key):
                return
            self._values[key] = value
        self._ensure_observable()

    def set_callback(
        self,
        callback: Callable[[], int | float | None],
        attributes: Mapping[str, str | bool | int | float] | None = None,
    ) -> None:
        key = _attrs_key(attributes or {})
        with self._lock:
            self._callbacks = [(existing, attrs) for existing, attrs in self._callbacks if attrs != key]
            self._callbacks.append((callback, key))
        self._ensure_observable()

    def inc(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        key = _attrs_key(attributes or {})
        with self._lock:
            if not self._can_store_key(key):
                return
            self._values[key] = self._values.get(key, 0) + amount
        self._ensure_observable()

    def dec(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        key = _attrs_key(attributes or {})
        with self._lock:
            if not self._can_store_key(key):
                return
            self._values[key] = self._values.get(key, 0) - amount
        self._ensure_observable()

    def observe(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        self.set(value, attributes)
