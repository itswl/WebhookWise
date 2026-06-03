from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final, TypedDict, cast

JsonObject = dict[str, Any]


class WebhookPayloadMetaKey(StrEnum):
    ADAPTER = "_adapter"


WEBHOOK_ADAPTER: Final = WebhookPayloadMetaKey.ADAPTER.value


class WebhookData(TypedDict, total=False):
    """Normalized webhook payload that moves through service-layer workflows.

    Raw webhook bodies stay intentionally loose at the ingress edge. Once a
    payload enters adapters, pipeline, analysis or forwarding, the common fields
    and project-owned metadata keys are declared here instead of being an
    unbounded ``dict[str, Any]`` contract.
    """

    source: str
    headers: JsonObject
    parsed_data: JsonObject
    body: Any
    timestamp: str
    client_ip: str | None
    Type: str
    RuleName: str
    AlertName: str
    MetricName: str
    Namespace: str
    Level: str
    Severity: str
    Resources: list[JsonObject]
    event: Any
    event_type: str
    alert_id: str
    alert_name: str
    service: str
    summary: str
    msg_type: str
    card: JsonObject
    labels: JsonObject
    annotations: JsonObject
    alerts: list[JsonObject]
    id: Any
    action: str
    status: str
    raw: Any
    first_trigger_time: str
    trigger_condition: str
    query_result: str
    analysis_result: JsonObject
    duration_seconds: float
    created_at: Any
    webhook_event_id: int
    openclaw_session_key: str
    openclaw_run_id: str
    engine: str
    poll_attempts: int
    next_poll_at: Any
    last_polled_at: Any
    _adapter: str
    _alert_identity: dict[str, str]
    _need_success_notify: bool
    _embedding: list[float]


_STRING_WEBHOOK_FIELDS: Final = frozenset(
    {
        "source",
        "timestamp",
        "Type",
        "RuleName",
        "AlertName",
        "MetricName",
        "Namespace",
        "Level",
        "Severity",
        "event_type",
        "alert_id",
        "alert_name",
        "service",
        "summary",
        "msg_type",
        "action",
        "status",
        "openclaw_session_key",
        "openclaw_run_id",
        "engine",
        "first_trigger_time",
        "trigger_condition",
        "query_result",
        WEBHOOK_ADAPTER,
    }
)
_MAPPING_WEBHOOK_FIELDS: Final = frozenset(
    {"headers", "parsed_data", "card", "labels", "annotations", "analysis_result", "_alert_identity"}
)
_LIST_MAPPING_WEBHOOK_FIELDS: Final = frozenset({"Resources", "alerts"})
_INT_WEBHOOK_FIELDS: Final = frozenset({"webhook_event_id", "poll_attempts"})
_FLOAT_WEBHOOK_FIELDS: Final = frozenset({"duration_seconds"})
_BOOL_WEBHOOK_FIELDS: Final = frozenset({"_need_success_notify"})
_LIST_FLOAT_WEBHOOK_FIELDS: Final = frozenset({"_embedding"})
_OPTIONAL_STRING_WEBHOOK_FIELDS: Final = frozenset({"client_ip"})
_DATETIME_WEBHOOK_FIELDS: Final = frozenset({"created_at", "next_poll_at", "last_polled_at"})
_JSON_WEBHOOK_FIELDS: Final = frozenset({"body", "event", "id", "raw"})


def _copy_json_compatible(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains non-string key: {key!r}")
            copied[key] = _copy_json_compatible(item, path=f"{path}.{key}")
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_copy_json_compatible(item, path=f"{path}[]") for item in value]
    raise ValueError(f"{path} contains non-JSON value: {type(value).__name__}")


def _copy_mapping_field(value: Any, *, field_name: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"WebhookData.{field_name} must be an object")
    return cast(JsonObject, _copy_json_compatible(value, path=f"WebhookData.{field_name}"))


def _copy_list_of_mappings(value: Any, *, field_name: str) -> list[JsonObject]:
    if not isinstance(value, list):
        raise ValueError(f"WebhookData.{field_name} must be a list")
    copied: list[JsonObject] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"WebhookData.{field_name}[{index}] must be an object")
        copied.append(cast(JsonObject, _copy_json_compatible(item, path=f"WebhookData.{field_name}[{index}]")))
    return copied


def webhook_data_from_mapping(data: Mapping[str, Any], *, strict: bool = True) -> WebhookData:
    """Validate and copy data into the declared WebhookData boundary.

    Adapter ingress can opt into ``strict=False`` when preserving source-native
    fields for downstream analysis. All other call sites reject undeclared keys
    by default so internal contracts fail fast instead of drifting silently.
    """

    if not isinstance(data, Mapping):
        raise TypeError("WebhookData input must be a mapping")

    normalized: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(f"WebhookData contains non-string key: {key!r}")
        if key in _STRING_WEBHOOK_FIELDS:
            if not isinstance(value, str):
                raise ValueError(f"WebhookData.{key} must be a string")
            normalized[key] = value
        elif key in _OPTIONAL_STRING_WEBHOOK_FIELDS:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"WebhookData.{key} must be a string or null")
            normalized[key] = value
        elif key in _MAPPING_WEBHOOK_FIELDS:
            normalized[key] = _copy_mapping_field(value, field_name=key)
        elif key in _LIST_MAPPING_WEBHOOK_FIELDS:
            normalized[key] = _copy_list_of_mappings(value, field_name=key)
        elif key in _INT_WEBHOOK_FIELDS:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"WebhookData.{key} must be an integer")
            normalized[key] = value
        elif key in _FLOAT_WEBHOOK_FIELDS:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"WebhookData.{key} must be a number")
            normalized[key] = float(value)
        elif key in _BOOL_WEBHOOK_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"WebhookData.{key} must be a boolean")
            normalized[key] = value
        elif key in _LIST_FLOAT_WEBHOOK_FIELDS:
            if not isinstance(value, list) or any(not isinstance(item, (int, float)) for item in value):
                raise ValueError(f"WebhookData.{key} must be a numeric list")
            normalized[key] = [float(item) for item in value]
        elif key in _DATETIME_WEBHOOK_FIELDS:
            normalized[key] = (
                value
                if isinstance(value, (datetime, date))
                else _copy_json_compatible(value, path=f"WebhookData.{key}")
            )
        elif key in _JSON_WEBHOOK_FIELDS:
            normalized[key] = _copy_json_compatible(value, path=f"WebhookData.{key}")
        else:
            if strict:
                raise ValueError(f"WebhookData.{key} is not declared")
            normalized[key] = _copy_json_compatible(value, path=f"WebhookData.{key}")
    return cast(WebhookData, normalized)
