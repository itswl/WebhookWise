"""Build compact alert identity context for LLM prompts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

JsonMap = Mapping[str, Any]

_EMPTY_VALUES: tuple[object, ...] = (None, "", {}, [])
_MAX_RESOURCES = 5
_MAX_METRICS = 8
_MAX_DIMENSIONS = 16


def _mapping(value: Any) -> JsonMap:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in _EMPTY_VALUES:
            return value
    return None


def _first_alert(data: JsonMap) -> JsonMap:
    alerts = _list(data.get("alerts"))
    if alerts and isinstance(alerts[0], Mapping):
        return alerts[0]
    return {}


def _first_resource(data: JsonMap) -> JsonMap:
    resources = _list(data.get("Resources"))
    if resources and isinstance(resources[0], Mapping):
        return resources[0]
    return {}


def _first_metric(resource: JsonMap) -> JsonMap:
    metrics = _list(resource.get("Metrics"))
    if metrics and isinstance(metrics[0], Mapping):
        return metrics[0]
    return {}


def _pick_label(labels: JsonMap, *keys: str) -> Any:
    for key in keys:
        if key in labels and labels[key] not in _EMPTY_VALUES:
            return labels[key]
    return None


def _put(out: dict[str, Any], key: str, value: Any) -> None:
    if value not in _EMPTY_VALUES:
        out[key] = value


def _dimension_map(resource: JsonMap) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dim in _list(resource.get("Dimensions"))[:_MAX_DIMENSIONS]:
        if not isinstance(dim, Mapping):
            continue
        name = _first_non_empty(dim.get("Name"), dim.get("NameCN"), dim.get("Description"))
        value = dim.get("Value")
        if name not in _EMPTY_VALUES and value not in _EMPTY_VALUES:
            out[str(name)] = value
    return out


def _resource_context(resource: JsonMap) -> dict[str, Any]:
    dimensions = _dimension_map(resource)
    out: dict[str, Any] = {}
    _put(out, "name", resource.get("Name"))
    _put(out, "id", _first_non_empty(resource.get("Id"), dimensions.get("ResourceID"), dimensions.get("InstanceId")))
    _put(out, "instance_id", resource.get("InstanceId"))
    _put(out, "region", resource.get("Region"))
    _put(out, "project", resource.get("ProjectName"))
    if dimensions:
        out["dimensions"] = dimensions
    return out


def _metric_context(metric: JsonMap) -> dict[str, Any]:
    out: dict[str, Any] = {}
    _put(out, "name", metric.get("Name"))
    _put(out, "description", _first_non_empty(metric.get("DescriptionCN"), metric.get("Description")))
    _put(out, "description_en", metric.get("DescriptionEN"))
    _put(out, "current_value", metric.get("CurrentValue"))
    _put(out, "threshold", metric.get("Threshold"))
    _put(out, "unit", metric.get("Unit"))
    _put(out, "trigger_condition", metric.get("TriggerCondition"))
    return out


def _resources_context(data: JsonMap) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for resource in _list(data.get("Resources"))[:_MAX_RESOURCES]:
        if isinstance(resource, Mapping):
            ctx = _resource_context(resource)
            if ctx:
                out.append(ctx)
    return out


def _metrics_context(data: JsonMap) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for resource in _list(data.get("Resources")):
        if not isinstance(resource, Mapping):
            continue
        for metric in _list(resource.get("Metrics")):
            if not isinstance(metric, Mapping):
                continue
            ctx = _metric_context(metric)
            if ctx:
                out.append(ctx)
            if len(out) >= _MAX_METRICS:
                return out
    return out


def build_alert_identity_context(source: str, data: JsonMap) -> dict[str, Any]:
    """Extract non-secret identifiers that distinguish one alert from another."""
    first_alert = _first_alert(data)
    labels = _mapping(first_alert.get("labels"))
    common_labels = _mapping(data.get("commonLabels"))
    group_labels = _mapping(data.get("groupLabels"))
    annotations = _mapping(first_alert.get("annotations"))
    common_annotations = _mapping(data.get("commonAnnotations"))
    resource = _first_resource(data)
    first_metric = _first_metric(resource)
    dimensions = _dimension_map(resource)
    stored_identity = _mapping(data.get("_alert_identity"))

    identity: dict[str, Any] = {}
    _put(identity, "source", _first_non_empty(stored_identity.get("source"), source))
    _put(identity, "status", _first_non_empty(data.get("status"), first_alert.get("status")))
    _put(
        identity,
        "severity",
        _first_non_empty(
            data.get("Level"),
            data.get("Severity"),
            stored_identity.get("severity"),
            _pick_label(labels, "severity", "internal_label_alert_level"),
            _pick_label(common_labels, "severity", "internal_label_alert_level"),
            _pick_label(group_labels, "severity", "internal_label_alert_level"),
        ),
    )
    _put(
        identity,
        "rule_name",
        _first_non_empty(
            data.get("RuleName"),
            data.get("AlertName"),
            data.get("alert_name"),
            stored_identity.get("name"),
            _pick_label(labels, "alertname", "alert_name", "internal_label_alertname"),
            _pick_label(common_labels, "alertname", "alert_name", "internal_label_alertname"),
        ),
    )
    _put(
        identity,
        "rule_id",
        _first_non_empty(
            data.get("RuleId"),
            data.get("alert_id"),
            _pick_label(labels, "internal_label_alert_id", "alert_id", "rule_id"),
            _pick_label(common_labels, "internal_label_alert_id", "alert_id", "rule_id"),
        ),
    )
    _put(identity, "account_id", data.get("AccountId"))
    _put(identity, "project", _first_non_empty(resource.get("ProjectName"), data.get("Project")))
    _put(identity, "cloud_project", data.get("Project"))
    _put(identity, "product_namespace", data.get("Namespace"))
    _put(identity, "sub_namespace", data.get("SubNamespace"))
    _put(identity, "region", _first_non_empty(resource.get("Region"), _pick_label(labels, "region", "zone", "az")))
    _put(identity, "cluster", _pick_label(labels, "cluster", "cluster_id", "kubernetes_cluster"))
    _put(
        identity,
        "namespace",
        _first_non_empty(
            _pick_label(labels, "namespace", "internal_label_namespace", "kubernetes_namespace"),
            _pick_label(common_labels, "namespace", "internal_label_namespace", "kubernetes_namespace"),
        ),
    )
    _put(
        identity,
        "service",
        _first_non_empty(
            stored_identity.get("service"),
            data.get("service"),
            _pick_label(
                labels,
                "service",
                "internal_label_service",
                "app",
                "app_kubernetes_io_name",
                "app.kubernetes.io/name",
                "k8s_app",
                "job",
                "container",
            ),
            _pick_label(common_labels, "service", "internal_label_service", "app", "job", "container"),
        ),
    )
    _put(
        identity,
        "resource_name",
        _first_non_empty(
            resource.get("Name"),
            resource.get("InstanceId"),
            stored_identity.get("resource"),
            _pick_label(labels, "pod", "instance", "host", "node", "container", "deployment"),
            _pick_label(common_labels, "pod", "instance", "host", "node", "container", "deployment"),
        ),
    )
    _put(
        identity,
        "resource_id",
        _first_non_empty(
            resource.get("Id"),
            dimensions.get("ResourceID"),
            resource.get("InstanceId"),
            _pick_label(labels, "uid", "resource_id", "internal_label_resource"),
            _pick_label(common_labels, "uid", "resource_id", "internal_label_resource"),
        ),
    )
    _put(
        identity,
        "metric_name",
        _first_non_empty(first_metric.get("Name"), data.get("MetricName"), _pick_label(labels, "__name__")),
    )
    _put(
        identity,
        "metric_description",
        _first_non_empty(first_metric.get("DescriptionCN"), first_metric.get("Description"), first_metric.get("DescriptionEN")),
    )
    _put(identity, "current_value", first_metric.get("CurrentValue"))
    _put(identity, "threshold", first_metric.get("Threshold"))
    _put(identity, "unit", first_metric.get("Unit"))
    _put(identity, "trigger_condition", _first_non_empty(first_metric.get("TriggerCondition"), data.get("RuleCondition")))
    _put(
        identity,
        "fingerprint",
        _first_non_empty(
            stored_identity.get("fingerprint"),
            first_alert.get("fingerprint"),
            _pick_label(labels, "fingerprint", "internal_label_alert_id"),
            _pick_label(common_labels, "fingerprint", "internal_label_alert_id"),
        ),
    )
    _put(identity, "host", _first_non_empty(_pick_label(labels, "host"), _pick_label(common_labels, "host")))
    _put(identity, "pod", _first_non_empty(_pick_label(labels, "pod"), _pick_label(common_labels, "pod")))
    _put(
        identity,
        "container",
        _first_non_empty(_pick_label(labels, "container"), _pick_label(common_labels, "container")),
    )
    _put(
        identity,
        "instance",
        _first_non_empty(_pick_label(labels, "instance"), _pick_label(common_labels, "instance"), resource.get("InstanceId")),
    )
    _put(identity, "job", _first_non_empty(_pick_label(labels, "job"), _pick_label(common_labels, "job")))
    _put(
        identity,
        "summary",
        _first_non_empty(
            data.get("summary"),
            annotations.get("summary"),
            annotations.get("description"),
            common_annotations.get("summary"),
            common_annotations.get("description"),
        ),
    )

    context: dict[str, Any] = {"identity": identity}
    resources = _resources_context(data)
    if resources:
        context["resources"] = resources
    metrics = _metrics_context(data)
    if metrics:
        context["metrics"] = metrics
    return context
