"""Deterministic resource metric risk classification."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.collections_utils import list_or_empty, mapping_or_empty
from services.webhooks.types import AnalysisResult

GPU_USED_METRIC = "GpuUsedUtilization"
GPU_MEMORY_METRIC = "GpuMemoryUsedUtilization"

GPU_USED_HIGH_THRESHOLD = 90.0
GPU_USED_WARNING_THRESHOLD = 80.0
GPU_MEMORY_HIGH_THRESHOLD = 95.0
GPU_MEMORY_WARNING_THRESHOLD = 90.0


@dataclass(frozen=True, slots=True)
class ResourceRiskAssessment:
    bucket: str
    reason: str


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().rstrip("%")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _metric_items(data: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for resource in list_or_empty(data.get("Resources")):
        resource_map = mapping_or_empty(resource)
        for metric in list_or_empty(resource_map.get("Metrics")):
            metric_map = mapping_or_empty(metric)
            if metric_map:
                yield metric_map

    top_level_name = data.get("MetricName") or data.get("Name")
    if top_level_name and any(key in data for key in ("CurrentValue", "current_value", "value")):
        yield data


def _is_gpu_context(data: Mapping[str, Any], metric_names: set[str]) -> bool:
    if metric_names & {GPU_USED_METRIC, GPU_MEMORY_METRIC}:
        return True

    text_fields = (
        data.get("RuleName"),
        data.get("AlertName"),
        data.get("SubNamespace"),
        data.get("Namespace"),
        data.get("RuleCondition"),
        data.get("MetricName"),
    )
    return any("gpu" in str(value or "").lower() for value in text_fields)


def _assess_gpu_risk(data: Mapping[str, Any]) -> ResourceRiskAssessment | None:
    metric_values: dict[str, list[float]] = {GPU_USED_METRIC: [], GPU_MEMORY_METRIC: []}
    metric_names: set[str] = set()

    for metric in _metric_items(data):
        name = str(metric.get("Name") or metric.get("MetricName") or "").strip()
        if name:
            metric_names.add(name)
        if name not in metric_values:
            continue
        value = _to_float(metric.get("CurrentValue") or metric.get("current_value") or metric.get("value"))
        if value is not None:
            metric_values[name].append(value)

    if not _is_gpu_context(data, metric_names):
        return None

    max_gpu_used = max(metric_values[GPU_USED_METRIC], default=None)
    max_gpu_memory = max(metric_values[GPU_MEMORY_METRIC], default=None)

    if max_gpu_used is not None and max_gpu_used >= GPU_USED_HIGH_THRESHOLD:
        return ResourceRiskAssessment(
            "gpu_high",
            f"GPU使用率{max_gpu_used:g}%达到高风险阈值{GPU_USED_HIGH_THRESHOLD:g}%",
        )
    if max_gpu_memory is not None and max_gpu_memory >= GPU_MEMORY_HIGH_THRESHOLD:
        return ResourceRiskAssessment(
            "gpu_high",
            f"GPU显存使用率{max_gpu_memory:g}%达到高风险阈值{GPU_MEMORY_HIGH_THRESHOLD:g}%",
        )
    if max_gpu_used is not None and max_gpu_used >= GPU_USED_WARNING_THRESHOLD:
        return ResourceRiskAssessment(
            "gpu_warning",
            f"GPU使用率{max_gpu_used:g}%超过预警阈值{GPU_USED_WARNING_THRESHOLD:g}%",
        )
    if max_gpu_memory is not None and max_gpu_memory >= GPU_MEMORY_WARNING_THRESHOLD:
        return ResourceRiskAssessment(
            "gpu_warning",
            f"GPU显存使用率{max_gpu_memory:g}%超过预警阈值{GPU_MEMORY_WARNING_THRESHOLD:g}%",
        )
    if max_gpu_used is not None or max_gpu_memory is not None:
        return ResourceRiskAssessment("gpu_normal", "GPU指标未达到预警阈值")
    return ResourceRiskAssessment("gpu_unknown", "GPU告警未提供可解析指标值")


def assess_resource_risk(data: Mapping[str, Any]) -> ResourceRiskAssessment | None:
    """Return the deterministic resource risk bucket for a parsed alert payload."""
    return _assess_gpu_risk(data)


def resource_dedup_bucket(data: Mapping[str, Any]) -> str | None:
    assessment = assess_resource_risk(data)
    return assessment.bucket if assessment else None


def apply_resource_importance_override(analysis: AnalysisResult, data: Mapping[str, Any]) -> AnalysisResult:
    """Promote clear resource saturation alerts to high even if the LLM/cache says otherwise."""
    assessment = assess_resource_risk(data)
    if assessment is None or assessment.bucket != "gpu_high":
        return analysis

    if str(analysis.get("importance", "")).strip().lower().rsplit(".", 1)[-1] == "high":
        return analysis

    updated: AnalysisResult = analysis.copy()
    updated["importance"] = "high"
    updated["_importance_override"] = assessment.bucket
    updated["_importance_override_reason"] = assessment.reason
    summary = updated.get("summary")
    if isinstance(summary, str) and summary.startswith(("🟠", "🟢")):
        updated["summary"] = f"🔴{summary[1:]}"
    risks = updated.get("risks")
    if isinstance(risks, list):
        updated["risks"] = [assessment.reason, *[str(risk) for risk in risks if str(risk) != assessment.reason]]
    else:
        updated["risks"] = [assessment.reason]
    return updated
