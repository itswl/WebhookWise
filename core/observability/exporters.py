"""OTLP exporter configuration shared by traces, metrics, and logs."""

from __future__ import annotations

import os
from typing import Any, Literal

Signal = Literal["traces", "metrics", "logs"]

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def otel_enabled() -> bool:
    raw = os.getenv("OTEL_ENABLED", "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def parse_headers(raw: str | None = None) -> dict[str, str]:
    value = (raw if raw is not None else os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")).strip()
    if not value:
        return {}
    headers: dict[str, str] = {}
    for part in value.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        if key:
            headers[key] = val.strip()
    return headers


def otlp_protocol() -> str:
    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    if protocol:
        return protocol
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return "http/protobuf" if endpoint.startswith(("http://", "https://")) else "grpc"


def otlp_timeout() -> int:
    raw = os.getenv("OTEL_EXPORTER_OTLP_TIMEOUT", "10").strip() or "10"
    return int(float(raw))


def otlp_insecure() -> bool:
    return env_flag("OTEL_EXPORTER_OTLP_INSECURE", default=False)


def signal_endpoint(signal: Signal) -> str:
    signal_specific = os.getenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT", "").strip()
    endpoint = signal_specific or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return ""
    protocol = otlp_protocol()
    if protocol in {"http", "http/protobuf", "http-protobuf"}:
        cleaned = endpoint.rstrip("/")
        if "/v1/" in cleaned:
            return cleaned
        return f"{cleaned}/v1/{signal}"
    return endpoint


def build_span_exporter() -> Any | None:
    protocol = otlp_protocol()
    endpoint = signal_endpoint("traces")
    if not endpoint:
        return None
    headers = parse_headers()
    timeout = otlp_timeout()
    if protocol in {"http", "http/protobuf", "http-protobuf"}:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            return None
        return OTLPSpanExporter(endpoint=endpoint, headers=headers or None, timeout=timeout)
    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError:
            return None
        return OTLPSpanExporter(endpoint=endpoint, headers=headers or None, timeout=timeout, insecure=otlp_insecure())
    return None


def build_metric_exporter() -> Any | None:
    protocol = otlp_protocol()
    endpoint = signal_endpoint("metrics")
    if not endpoint:
        return None
    headers = parse_headers()
    timeout = otlp_timeout()
    if protocol in {"http", "http/protobuf", "http-protobuf"}:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        except ImportError:
            return None
        return OTLPMetricExporter(endpoint=endpoint, headers=headers or None, timeout=timeout)
    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        except ImportError:
            return None
        return OTLPMetricExporter(endpoint=endpoint, headers=headers or None, timeout=timeout, insecure=otlp_insecure())
    return None


def build_log_exporter() -> Any | None:
    protocol = otlp_protocol()
    endpoint = signal_endpoint("logs")
    if not endpoint:
        return None
    headers = parse_headers()
    timeout = otlp_timeout()
    if protocol in {"http", "http/protobuf", "http-protobuf"}:
        try:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        except ImportError:
            return None
        return OTLPLogExporter(endpoint=endpoint, headers=headers or None, timeout=timeout)
    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        except ImportError:
            return None
        return OTLPLogExporter(endpoint=endpoint, headers=headers or None, timeout=timeout, insecure=otlp_insecure())
    return None
