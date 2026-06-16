"""Noise-reduction stage for webhook processing."""

import asyncio
import time
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from core.datetime_utils import utcnow
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
_NOISE_CONTEXT_ERRORS = (OSError, RuntimeError, SQLAlchemyError, ValueError)
_SUPPRESSED_RECORD_ERRORS = (OSError, RuntimeError, SQLAlchemyError, ValueError)


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
            return NoiseReductionContext("standalone", None, 0.0, False, "Smart noise reduction is not enabled", 0, ())
        now = utcnow()
        try:
            recent = await list_recent_alert_contexts(alert_hash, now, policy.window_minutes)
        except _NOISE_CONTEXT_ERRORS as e:
            logger.warning("[Pipeline] Failed to load recent alert context, noise reduction will be skipped: %s", e)
            recent = []
        curr = AlertContext(
            None,
            source,
            normalize_importance(analysis.get("importance", "medium")),
            parsed,
            analysis,
            now,
        )
        # Scoring is synchronous and CPU-bound (regex tokenization + similarity
        # over up to ~100 candidates); offload it so the worker event loop is
        # not stalled per non-duplicate webhook.
        dec = await asyncio.to_thread(
            analyze_noise_reduction,
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
                "[Noise] Suppressing forward relation=%s root_cause_id=%s confidence=%.2f reason=%s",
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
            except _SUPPRESSED_RECORD_ERRORS as e:
                logger.warning("[Noise] Failed to write suppressed_records: %s", e)
        elif dec.relation != "standalone":
            logger.debug(
                "[Noise] Correlated but not suppressed relation=%s root_cause_id=%s confidence=%.2f",
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
