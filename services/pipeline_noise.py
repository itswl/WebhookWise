"""降噪与持久化模块，从 pipeline.py 提取。"""

from datetime import datetime, timedelta

from sqlalchemy import select

from api import (
    AnalysisResolution,
    NoiseReductionContext,
    PersistedEventContext,
    WebhookRequestContext,
)
from core.config import Config
from core.logger import logger
from crud.webhook import save_webhook_data
from db.session import session_scope
from models import WebhookEvent
from services.alert_noise_reduction import AlertContext, analyze_noise_reduction


def _default_noise_context() -> NoiseReductionContext:
    return NoiseReductionContext(
        relation="standalone",
        root_cause_event_id=None,
        confidence=0.0,
        suppress_forward=False,
        reason="智能降噪未启用",
        related_alert_count=0,
        related_alert_ids=[],
    )


def _build_alert_context(
    event_id: int | None,
    source: str,
    parsed_data: dict,
    analysis: dict,
    timestamp: datetime,
    alert_hash: str | None = None,
    importance: str | None = None,
) -> AlertContext:
    derived_importance = str(importance or analysis.get("importance") or "").lower().strip()
    if derived_importance not in {"high", "medium", "low"}:
        derived_importance = "medium"

    return AlertContext(
        event_id=event_id,
        source=source,
        importance=derived_importance,
        parsed_data=parsed_data if isinstance(parsed_data, dict) else {},
        analysis=analysis if isinstance(analysis, dict) else {},
        timestamp=timestamp,
        alert_hash=alert_hash,
    )


async def _load_recent_alert_contexts(current_hash: str, current_time: datetime) -> list[AlertContext]:
    window_minutes = max(1, Config.NOISE_REDUCTION_WINDOW_MINUTES)
    time_threshold = current_time - timedelta(minutes=window_minutes)

    try:
        async with session_scope() as session:
            stmt = (
                select(WebhookEvent)
                .filter(WebhookEvent.timestamp >= time_threshold, WebhookEvent.timestamp <= current_time)
                .order_by(WebhookEvent.timestamp.desc())
                .limit(100)
            )
            result = await session.execute(stmt)
            events = result.scalars().all()

    except Exception as e:
        logger.warning(f"加载降噪候选告警失败: {e}")
        return []

    contexts: list[AlertContext] = []
    for event in events:
        if event.alert_hash == current_hash:
            continue
        contexts.append(
            _build_alert_context(
                event_id=event.id,
                source=event.source,
                parsed_data=event.parsed_data or {},
                analysis=event.ai_analysis or {},
                timestamp=event.timestamp or datetime.now(),
                alert_hash=event.alert_hash,
                importance=event.importance,
            )
        )

    return contexts


async def _compute_noise_reduction(
    *,
    alert_hash: str,
    source: str,
    parsed_data: dict,
    analysis_result: dict,
) -> NoiseReductionContext:
    if not Config.ENABLE_ALERT_NOISE_REDUCTION:
        return _default_noise_context()

    now = datetime.now()
    current_ctx = _build_alert_context(
        event_id=None,
        source=source,
        parsed_data=parsed_data,
        analysis=analysis_result,
        timestamp=now,
        alert_hash=alert_hash,
    )

    recent_contexts = await _load_recent_alert_contexts(alert_hash, now)
    decision = analyze_noise_reduction(
        current_ctx,
        recent_contexts,
        window_minutes=max(1, Config.NOISE_REDUCTION_WINDOW_MINUTES),
        min_confidence=max(0.0, min(1.0, Config.ROOT_CAUSE_MIN_CONFIDENCE)),
        suppress_derived=Config.SUPPRESS_DERIVED_ALERT_FORWARD,
    )

    return NoiseReductionContext(
        relation=decision.relation,
        root_cause_event_id=decision.root_cause_event_id,
        confidence=decision.confidence,
        suppress_forward=decision.suppress_forward,
        reason=decision.reason,
        related_alert_count=decision.related_alert_count,
        related_alert_ids=decision.related_alert_ids,
    )


def _apply_noise_metadata(analysis_result: dict, noise_context: NoiseReductionContext) -> dict:
    merged = dict(analysis_result)
    merged["noise_reduction"] = {
        "relation": noise_context.relation,
        "root_cause_event_id": noise_context.root_cause_event_id,
        "confidence": noise_context.confidence,
        "suppress_forward": noise_context.suppress_forward,
        "reason": noise_context.reason,
        "related_alert_count": noise_context.related_alert_count,
        "related_alert_ids": noise_context.related_alert_ids,
    }
    return merged


async def persist_webhook_with_noise_context(
    *,
    request_context: WebhookRequestContext,
    analysis_resolution: AnalysisResolution,
    alert_hash: str,
    event_id: int | None = None,
) -> PersistedEventContext:
    """对外入口：计算降噪并持久化 webhook 数据。"""
    noise_context = await _compute_noise_reduction(
        alert_hash=alert_hash,
        source=request_context.source,
        parsed_data=request_context.parsed_data,
        analysis_result=analysis_resolution.analysis_result,
    )

    analysis_with_noise = _apply_noise_metadata(analysis_resolution.analysis_result, noise_context)
    save_result = await save_webhook_data(
        data=request_context.parsed_data,
        source=request_context.source,
        raw_payload=request_context.payload,
        headers=request_context.headers,
        client_ip=request_context.client_ip,
        ai_analysis=analysis_with_noise,
        alert_hash=alert_hash,
        is_duplicate=analysis_resolution.is_duplicate or analysis_resolution.beyond_window,
        original_event=analysis_resolution.original_event,
        beyond_window=analysis_resolution.beyond_window,
        reanalyzed=analysis_resolution.reanalyzed,
        event_id=event_id,
    )

    return PersistedEventContext(save_result=save_result, noise_context=noise_context)
