"""OTLP exporter configuration shared by traces, metrics, and logs."""

from __future__ import annotations

import os
from typing import Any, Literal

Signal = Literal["traces", "metrics", "logs"]

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def otel_enabled() -> bool:
    return env_flag("OTEL_ENABLED", default=False)


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
    return "grpc"


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


def _build_exporter(signal: Signal, http_path: str, grpc_path: str, class_name: str) -> Any | None:
    """Load an OTLP exporter class lazily based on protocol and instantiate it."""
    endpoint = signal_endpoint(signal)
    if not endpoint:
        return None
    protocol = otlp_protocol()
    headers = parse_headers()
    timeout = otlp_timeout()
    if protocol in {"http", "http/protobuf", "http-protobuf"}:
        try:
            module = __import__(http_path, fromlist=[class_name])
        except ImportError:
            return None
        return getattr(module, class_name)(endpoint=endpoint, headers=headers or None, timeout=timeout)
    if protocol == "grpc":
        try:
            module = __import__(grpc_path, fromlist=[class_name])
        except ImportError:
            return None
        return getattr(module, class_name)(
            endpoint=endpoint, headers=headers or None, timeout=timeout, insecure=otlp_insecure()
        )
    return None


def build_span_exporter() -> Any | None:
    return _build_exporter(
        "traces",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "OTLPSpanExporter",
    )


def build_metric_exporter() -> Any | None:
    return _build_exporter(
        "metrics",
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
        "OTLPMetricExporter",
    )


def build_log_exporter() -> Any | None:
    return _build_exporter(
        "logs",
        "opentelemetry.exporter.otlp.proto.http._log_exporter",
        "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
        "OTLPLogExporter",
    )
