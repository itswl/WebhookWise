from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.collections_utils import dict_or_empty, list_or_empty
from core.logger import get_logger
from services.analysis.analysis_policies import NoiseScoringConfig
from services.webhooks.types import ANALYSIS_EMBEDDING, AnalysisResult, NoiseReductionContext

logger = get_logger("analysis.noise_reduction")


AlertPayload = dict[str, Any]


@dataclass(frozen=True)
class AlertContext:
    event_id: int | None
    source: str
    importance: str
    parsed_data: AlertPayload
    analysis: AnalysisResult
    timestamp: datetime


DEFAULT_SCORING_CONFIG = NoiseScoringConfig(
    source_weight=0.15,
    resource_weight=0.45,
    semantic_weight=0.25,
    severity_weight=0.10,
    time_weight=0.20,
    severity_downgrade_score=0.03,
    related_min_confidence=0.35,
)


def _tokenize_text(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).lower().strip()
        if not text:
            continue

        # English/numeric token
        for token in re.findall(r"[a-z0-9_.-]{3,}", text):
            tokens.add(token)

        # Simple Chinese fragment token
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            tokens.add(token)

    if tokens and logger.isEnabledFor(logging.DEBUG):
        logger.debug("[Noise] Text tokenization result: count=%d, sample=%r", len(tokens), list(tokens)[:5])
    return tokens


def _extract_resource_ids(parsed_data: AlertPayload) -> set[str]:
    ids: set[str] = set()

    direct_keys = ["resource_id", "ResourceID", "InstanceId", "instance", "host", "pod"]
    for key in direct_keys:
        value = parsed_data.get(key)
        if value:
            ids.add(str(value).strip().lower())

    resources = list_or_empty(parsed_data.get("Resources"))
    for item in resources:
        if not isinstance(item, dict):
            continue
        for key in ("InstanceId", "Id", "id"):
            value = item.get(key)
            candidate = str(value).strip() if value else ""
            if candidate:
                ids.add(candidate.lower())
                break

    alerts = list_or_empty(parsed_data.get("alerts"))
    if alerts:
        first_alert = alerts[0] if isinstance(alerts[0], dict) else {}
        labels = dict_or_empty(first_alert.get("labels"))
        for key in ("instance", "pod", "host", "service", "namespace"):
            value = labels.get(key)
            if value:
                ids.add(str(value).strip().lower())

    return {x for x in ids if x}


def _extract_features(ctx: AlertContext) -> tuple[set[str], set[str]]:
    parsed = dict_or_empty(ctx.parsed_data)
    analysis = dict_or_empty(ctx.analysis)

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

    alerts = list_or_empty(parsed.get("alerts"))
    if alerts:
        first_alert = alerts[0] if isinstance(alerts[0], dict) else {}
        labels = dict_or_empty(first_alert.get("labels"))
        annotations = dict_or_empty(first_alert.get("annotations"))
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


def _semantic_similarity(
    current: AlertContext, candidate: AlertContext, current_tokens: set[str], candidate_tokens: set[str]
) -> float:
    def _get_embedding(ctx: AlertContext) -> list[float] | None:
        for container in (dict_or_empty(ctx.analysis), dict_or_empty(ctx.parsed_data)):
            value = container.get(ANALYSIS_EMBEDDING) or container.get("embedding")
            if not isinstance(value, list) or not value:
                continue
            vector: list[float] = []
            for item in value:
                if not isinstance(item, (int, float)):
                    vector = []
                    break
                vector.append(float(item))
            if vector:
                return vector
        return None

    emb_a, emb_b = _get_embedding(current), _get_embedding(candidate)
    embedding_score = 0.0
    if emb_a and emb_b and len(emb_a) == len(emb_b):
        dot = sum(x * y for x, y in zip(emb_a, emb_b, strict=True))
        norm_a = math.sqrt(sum(x * x for x in emb_a))
        norm_b = math.sqrt(sum(y * y for y in emb_b))
        if norm_a > 0 and norm_b > 0:
            embedding_score = max(0.0, min(1.0, dot / (norm_a * norm_b)))
    token_score = _jaccard(current_tokens, candidate_tokens)
    return max(embedding_score, token_score)


def score_candidate(
    current: AlertContext,
    candidate: AlertContext,
    window_minutes: int,
    scoring_config: NoiseScoringConfig = DEFAULT_SCORING_CONFIG,
    *,
    current_features: tuple[set[str], set[str]] | None = None,
) -> float:
    if candidate.timestamp > current.timestamp:
        return 0.0

    elapsed = (current.timestamp - candidate.timestamp).total_seconds()
    window_seconds = max(window_minutes, 1) * 60

    # The current alert's features are identical across candidates; accept a
    # precomputed pair to avoid re-tokenizing it N times in the scoring loop.
    current_resources, current_tokens = current_features if current_features is not None else _extract_features(current)
    candidate_resources, candidate_tokens = _extract_features(candidate)

    source_score = scoring_config.source_weight if current.source == candidate.source else 0.0
    resource_score = scoring_config.resource_weight * _jaccard(current_resources, candidate_resources)
    semantic_score = scoring_config.semantic_weight * _semantic_similarity(
        current, candidate, current_tokens, candidate_tokens
    )

    imp_map = {"high": 1.0, "medium": 0.6, "low": 0.2}
    candidate_level = imp_map.get(str(candidate.importance).lower(), 0.6)
    current_level = imp_map.get(str(current.importance).lower(), 0.6)
    severity_score = (
        scoring_config.severity_weight if candidate_level >= current_level else scoring_config.severity_downgrade_score
    )

    time_score = scoring_config.time_weight * (1 - (elapsed / window_seconds))

    total = source_score + resource_score + semantic_score + severity_score + time_score
    if total < 0:
        return 0.0
    if total > 1:
        return 1.0
    return total


def _collect_related(
    current: AlertContext,
    recent_alerts: Iterable[AlertContext],
    window_minutes: int,
    scoring_config: NoiseScoringConfig,
) -> list[tuple[AlertContext, float]]:
    scored: list[tuple[AlertContext, float]] = []
    current_features = _extract_features(current)  # computed once, reused per candidate
    for alert in recent_alerts:
        score = score_candidate(current, alert, window_minutes, scoring_config, current_features=current_features)
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
    scoring_config: NoiseScoringConfig = DEFAULT_SCORING_CONFIG,
) -> NoiseReductionContext:
    """
    Analyze noise reduction.

    Args:
        current: Current alert context
        recent_alerts: List of recent alerts
        window_minutes: Time window (minutes)
        min_confidence: Minimum confidence threshold
        suppress_derived: Whether to suppress forwarding of derived alerts
    """
    logger.debug("Using fixed threshold: %.4f", min_confidence)

    # Collect related alerts
    recent_alerts_list = list(recent_alerts)
    scored = _collect_related(current, recent_alerts_list, window_minutes, scoring_config)

    if not scored:
        logger.info("[Noise] Noise-reduction decision: relation=standalone")
        return NoiseReductionContext("standalone", None, 0.0, False, "No correlatable alert relationship found", 0, ())

    related = [(alert, score) for alert, score in scored if score >= scoring_config.related_min_confidence]
    related_ids = [alert.event_id for alert, _ in related if alert.event_id is not None]

    best_alert, best_score = scored[0]

    # Root-cause determination
    if best_alert.event_id is not None and best_score >= min_confidence:
        reason = f"Highly correlated with alert #{best_alert.event_id} (confidence {best_score:.2f})"

        logger.info("[Noise] Noise-reduction decision: relation=derived, confidence=%.2f, suppress=%s", best_score, suppress_derived)
        return NoiseReductionContext(
            relation="derived",
            root_cause_event_id=best_alert.event_id,
            confidence=round(best_score, 4),
            suppress_forward=suppress_derived,
            reason=reason,
            related_alert_count=len(related_ids),
            related_alert_ids=tuple(related_ids),
        )

    # Alert-storm detection
    if current.importance == "high" and len(related_ids) >= 2:
        reason = f"Alert storm detected; correlated {len(related_ids)} nearby alerts"

        logger.info("[Noise] Noise-reduction decision: relation=root_cause, count=%d", len(related_ids))
        return NoiseReductionContext(
            relation="root_cause",
            root_cause_event_id=current.event_id,
            confidence=round(best_score, 4),
            suppress_forward=False,
            reason=reason,
            related_alert_count=len(related_ids),
            related_alert_ids=tuple(related_ids),
        )

    return NoiseReductionContext(
        relation="standalone",
        root_cause_event_id=None,
        confidence=round(best_score, 4),
        suppress_forward=False,
        reason="Weakly correlated alerts exist, but the root-cause determination threshold was not reached",
        related_alert_count=len(related_ids),
        related_alert_ids=tuple(related_ids),
    )
