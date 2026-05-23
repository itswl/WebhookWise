"""Noise-reduction stage for webhook processing."""

import time
from datetime import datetime
from typing import Any

from core.logger import get_logger
from core.observability.metrics import (
    WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS,
    WEBHOOK_NOISE_EVALUATIONS_TOTAL,
    sanitize_source,
)
from db.session import session_scope
from models import SuppressedRecord
from services.analysis.noise_reduction import AlertContext, analyze_noise_reduction
from services.webhooks.decisioning import normalize_importance
from services.webhooks.policies import NoiseReductionPolicy
from services.webhooks.repository import list_recent_alert_contexts
from services.webhooks.types import AnalysisResult, NoiseReductionContext

logger = get_logger("webhooks.noise_stage")


async def compute_noise(
    alert_hash: str,
    source: str,
    parsed: dict[str, Any],
    analysis: AnalysisResult,
    *,
    policy: NoiseReductionPolicy | None = None,
) -> NoiseReductionContext:
    started = time.perf_counter()
    metric_source = sanitize_source(source)
    relation = "standalone"
    suppressed = "false"
    policy = policy or NoiseReductionPolicy.from_config()
    try:
        if not policy.enabled:
            return NoiseReductionContext("standalone", None, 0.0, False, "智能降噪未启用", 0, [])
        now = datetime.now()
        try:
            recent = await list_recent_alert_contexts(alert_hash, now, policy.window_minutes)
        except Exception as e:
            logger.warning("[Pipeline] 加载近期告警上下文失败，降噪将跳过: %s", e)
            recent = []
        curr = AlertContext(
            None,
            source,
            normalize_importance(analysis.get("importance", "medium")),
            parsed,
            analysis,
            now,
            alert_hash,
        )
        dec = analyze_noise_reduction(
            curr,
            recent,
            window_minutes=policy.window_minutes,
            min_confidence=policy.root_cause_min_confidence,
            suppress_derived=policy.suppress_derived_forward,
            scoring_config=policy.scoring_config,
        )
        relation = dec.relation
        suppressed = str(dec.suppress_forward).lower()
        if dec.suppress_forward:
            logger.info(
                "[Noise] 抑制转发 relation=%s root_cause_id=%s confidence=%.2f reason=%s",
                dec.relation,
                dec.root_cause_event_id,
                dec.confidence,
                dec.reason,
            )
            try:
                async with session_scope() as session:
                    session.add(
                        SuppressedRecord(
                            alert_hash=alert_hash,
                            source=source,
                            relation=dec.relation,
                            root_cause_event_id=dec.root_cause_event_id,
                            reason=str(dec.reason or "")[:500],
                            related_alert_ids=list(dec.related_alert_ids or []),
                            confidence=float(dec.confidence or 0.0),
                            created_at=now,
                        )
                    )
            except Exception as e:
                logger.warning("[Noise] 写入 suppressed_records 失败: %s", e)
        elif dec.relation != "standalone":
            logger.debug(
                "[Noise] 关联但不抑制 relation=%s root_cause_id=%s confidence=%.2f",
                dec.relation,
                dec.root_cause_event_id,
                dec.confidence,
            )
        return dec
    finally:
        WEBHOOK_NOISE_EVALUATIONS_TOTAL.labels(metric_source, relation, suppressed).inc()
        WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS.labels(metric_source, relation, suppressed).observe(
            time.perf_counter() - started
        )
