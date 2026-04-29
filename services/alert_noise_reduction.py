from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


AlertPayload = dict[str, Any]
AnalysisResult = dict[str, Any]


@dataclass(frozen=True)
class AlertContext:
    event_id: int | None
    source: str
    importance: str
    parsed_data: AlertPayload
    analysis: AnalysisResult
    timestamp: datetime
    alert_hash: str | None = None


@dataclass(frozen=True)
class NoiseReductionDecision:
    relation: str
    root_cause_event_id: int | None
    confidence: float
    suppress_forward: bool
    reason: str
    related_alert_count: int
    related_alert_ids: list[int]


def default_decision() -> NoiseReductionDecision:
    return NoiseReductionDecision(
        relation="standalone",
        root_cause_event_id=None,
        confidence=0.0,
        suppress_forward=False,
        reason="未发现可关联的告警关系",
        related_alert_count=0,
        related_alert_ids=[],
    )


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _tokenize_text(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).lower().strip()
        if not text:
            continue

        # 英文/数字 token
        for token in re.findall(r"[a-z0-9_.-]{3,}", text):
            tokens.add(token)

        # 简单中文片段 token
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            tokens.add(token)

    if tokens:
        logger.debug(f"[Noise] 文本分词结果: count={len(tokens)}, sample={list(tokens)[:5]}")
    return tokens


def _pick_first(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_resource_ids(parsed_data: AlertPayload) -> set[str]:
    ids: set[str] = set()

    direct_keys = ["resource_id", "ResourceID", "InstanceId", "instance", "host", "pod"]
    for key in direct_keys:
        value = parsed_data.get(key)
        if value:
            ids.add(str(value).strip().lower())

    resources = _safe_list(parsed_data.get("Resources"))
    for item in resources:
        if not isinstance(item, dict):
            continue
        candidate = _pick_first(item.get("InstanceId"), item.get("Id"), item.get("id"))
        if candidate:
            ids.add(candidate.lower())

    alerts = _safe_list(parsed_data.get("alerts"))
    if alerts:
        first_alert = alerts[0] if isinstance(alerts[0], dict) else {}
        labels = _safe_dict(first_alert.get("labels"))
        for key in ("instance", "pod", "host", "service", "namespace"):
            value = labels.get(key)
            if value:
                ids.add(str(value).strip().lower())

    return {x for x in ids if x}


def _extract_features(ctx: AlertContext) -> tuple[set[str], set[str]]:
    parsed = _safe_dict(ctx.parsed_data)
    analysis = _safe_dict(ctx.analysis)

    resource_ids = _extract_resource_ids(parsed)

    primary_fields = [
        parsed.get("RuleName"),
        parsed.get("alert_name"),
        parsed.get("event_type"),
        parsed.get("event"),
        parsed.get("MetricName"),
        parsed.get("Type"),
        parsed.get("service"),
        analysis.get("event_type"),
        analysis.get("summary"),
        analysis.get("root_cause"),
        analysis.get("impact_scope"),
    ]

    alerts = _safe_list(parsed.get("alerts"))
    if alerts:
        first_alert = alerts[0] if isinstance(alerts[0], dict) else {}
        labels = _safe_dict(first_alert.get("labels"))
        annotations = _safe_dict(first_alert.get("annotations"))
        primary_fields.extend(
            [
                labels.get("alertname"),
                labels.get("severity"),
                labels.get("service"),
                annotations.get("summary"),
                annotations.get("description"),
            ]
        )

    tokens = _tokenize_text(*primary_fields)
    return resource_ids, tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union_size = len(a | b)
    if union_size == 0:
        return 0.0
    return len(a & b) / union_size


def _importance_score(value: str) -> float:
    mapping = {"high": 1.0, "medium": 0.6, "low": 0.2}
    return mapping.get(str(value).lower(), 0.6)


def score_candidate(current: AlertContext, candidate: AlertContext, window_minutes: int) -> float:
    if candidate.timestamp > current.timestamp:
        return 0.0

    elapsed = (current.timestamp - candidate.timestamp).total_seconds()
    window_seconds = max(window_minutes, 1) * 60
    if elapsed > window_seconds:
        return 0.0

    current_resources, current_tokens = _extract_features(current)
    candidate_resources, candidate_tokens = _extract_features(candidate)

    source_score = 0.15 if current.source == candidate.source else 0.0
    resource_score = 0.45 * _jaccard(current_resources, candidate_resources)
    token_score = 0.25 * _jaccard(current_tokens, candidate_tokens)

    candidate_level = _importance_score(candidate.importance)
    current_level = _importance_score(current.importance)
    severity_score = 0.1 if candidate_level >= current_level else 0.03

    time_score = 0.2 * (1 - (elapsed / window_seconds))

    total = source_score + resource_score + token_score + severity_score + time_score
    if total < 0:
        return 0.0
    if total > 1:
        return 1.0
    return total


def _collect_related(
    current: AlertContext,
    recent_alerts: Iterable[AlertContext],
    window_minutes: int,
) -> list[tuple[AlertContext, float]]:
    scored: list[tuple[AlertContext, float]] = []
    for alert in recent_alerts:
        score = score_candidate(current, alert, window_minutes)
        if score > 0:
            scored.append((alert, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def analyze_noise_reduction(
    current: AlertContext,
    recent_alerts: Iterable[AlertContext],
    *,
    window_minutes: int,
    min_confidence: float,
    suppress_derived: bool,
) -> NoiseReductionDecision:
    """
    分析噪声降低

    Args:
        current: 当前告警上下文
        recent_alerts: 近期告警列表
        window_minutes: 时间窗口（分钟）
        min_confidence: 最小置信度阈值
        suppress_derived: 是否抑制衍生告警转发
    """
    logger.debug(f"使用固定阈值: {min_confidence:.4f}")

    # 收集相关告警
    recent_alerts_list = list(recent_alerts)
    scored = _collect_related(current, recent_alerts_list, window_minutes)

    if not scored:
        logger.info("[Noise] 降噪决策: relation=standalone")
        return default_decision()

    related = [(alert, score) for alert, score in scored if score >= 0.35]
    related_ids = [alert.event_id for alert, _ in related if alert.event_id is not None]

    best_alert, best_score = scored[0]

    # 根因判定
    if best_alert.event_id is not None and best_score >= min_confidence:
        reason = f"与告警#{best_alert.event_id} 高相关（置信度 {best_score:.2f}）"

        logger.info(f"[Noise] 降噪决策: relation=derived, confidence={best_score:.2f}, suppress={suppress_derived}")
        return NoiseReductionDecision(
            relation="derived",
            root_cause_event_id=best_alert.event_id,
            confidence=round(best_score, 4),
            suppress_forward=suppress_derived,
            reason=reason,
            related_alert_count=len(related_ids),
            related_alert_ids=related_ids,
        )

    # 告警风暴检测
    if current.importance == "high" and len(related_ids) >= 2:
        reason = f"检测到告警风暴，已关联 {len(related_ids)} 条近邻告警"

        logger.info(f"[Noise] 降噪决策: relation=root_cause, count={len(related_ids)}")
        return NoiseReductionDecision(
            relation="root_cause",
            root_cause_event_id=current.event_id,
            confidence=round(best_score, 4),
            suppress_forward=False,
            reason=reason,
            related_alert_count=len(related_ids),
            related_alert_ids=related_ids,
        )

    return NoiseReductionDecision(
        relation="standalone",
        root_cause_event_id=None,
        confidence=round(best_score, 4),
        suppress_forward=False,
        reason="存在弱关联告警，但未达到根因判定阈值",
        related_alert_count=len(related_ids),
        related_alert_ids=related_ids,
    )
