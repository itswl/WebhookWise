"""Noise-reduction stage for webhook processing."""

from datetime import datetime
from typing import Any

from core.logger import logger
from services.analysis.noise_reduction import AlertContext, analyze_noise_reduction
from services.webhooks.decisioning import normalize_importance
from services.webhooks.policies import NoiseReductionPolicy
from services.webhooks.repository import list_recent_alert_contexts
from services.webhooks.types import NoiseReductionContext


async def compute_noise(
    alert_hash: str,
    source: str,
    parsed: dict[str, Any],
    analysis: dict[str, Any],
    *,
    policy: NoiseReductionPolicy | None = None,
) -> NoiseReductionContext:
    policy = policy or NoiseReductionPolicy.from_config()
    if not policy.enabled:
        return NoiseReductionContext("standalone", None, 0.0, False, "智能降噪未启用", 0, [])
    now = datetime.now()
    try:
        recent = await list_recent_alert_contexts(alert_hash, now, policy.window_minutes)
    except Exception as e:
        logger.warning("[Pipeline] 加载近期告警上下文失败，降噪将跳过: %s", e)
        recent = []
    curr = AlertContext(
        None, source, normalize_importance(analysis.get("importance", "medium")), parsed, analysis, now, alert_hash
    )
    dec = analyze_noise_reduction(
        curr,
        recent,
        window_minutes=policy.window_minutes,
        min_confidence=policy.root_cause_min_confidence,
        suppress_derived=policy.suppress_derived_forward,
        scoring_config=policy.scoring_config,
    )
    if dec.suppress_forward:
        logger.info(
            "[Noise] 抑制转发 relation=%s root_cause_id=%s confidence=%.2f reason=%s",
            dec.relation,
            dec.root_cause_event_id,
            dec.confidence,
            dec.reason,
        )
    elif dec.relation != "standalone":
        logger.debug(
            "[Noise] 关联但不抑制 relation=%s root_cause_id=%s confidence=%.2f",
            dec.relation,
            dec.root_cause_event_id,
            dec.confidence,
        )
    return NoiseReductionContext(
        dec.relation,
        dec.root_cause_event_id,
        dec.confidence,
        dec.suppress_forward,
        dec.reason,
        dec.related_alert_count,
        dec.related_alert_ids,
    )
