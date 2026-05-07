"""
services/noise_context.py
===============================
告警智能降噪相关辅助函数。
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from db.session import session_scope
from models import WebhookEvent
from services.alert_noise_reduction import (
    AlertContext,
    NoiseReductionDecision,
    analyze_noise_reduction,
    default_decision,
)

logger = logging.getLogger(__name__)


# ── 告警上下文构建 ─────────────────────────────────────────────────────────────


def _default_noise_decision() -> NoiseReductionDecision:
    return default_decision()


def _build_alert_context(current_event: WebhookEvent, current_time: datetime) -> AlertContext:
    """
    从当前事件构建 AlertContext，供 analyze_noise_reduction 使用。
    提取当前事件的字段用于关联分析。
    """
    parsed_raw = current_event.parsed_data
    parsed: dict[str, Any] = dict(parsed_raw) if isinstance(parsed_raw, dict) else {}
    analysis_raw = current_event.ai_analysis
    analysis: dict[str, Any] = dict(analysis_raw) if isinstance(analysis_raw, dict) else {}
    return AlertContext(
        event_id=current_event.id,
        source=current_event.source or "unknown",
        importance=current_event.importance or "medium",
        parsed_data=parsed,
        analysis=analysis,
        timestamp=current_time,
        alert_hash=current_event.alert_hash,
    )


async def _load_recent_alert_contexts(
    current_hash: str, current_time: datetime, window_minutes: int = 5
) -> list[AlertContext]:
    """
    加载当前告警前后窗口内的相关告警上下文。
    用于根因分析的时间窗口关联。
    """
    recent: list[AlertContext] = []
    window_start = current_time - timedelta(minutes=window_minutes)

    async with session_scope() as session:
        # 投影查询：仅加载需要的字段，避免拉取 raw_payload 等大字段
        result = await session.execute(
            select(
                WebhookEvent.id,
                WebhookEvent.alert_hash,
                WebhookEvent.importance,
                WebhookEvent.source,
                WebhookEvent.timestamp,
                WebhookEvent.parsed_data,
                WebhookEvent.ai_analysis,
            ).filter(WebhookEvent.timestamp >= window_start, WebhookEvent.timestamp <= current_time)
        )
        rows = result.all()

        for row in rows:
            parsed = dict(row.parsed_data) if isinstance(row.parsed_data, dict) else {}
            analysis = dict(row.ai_analysis) if isinstance(row.ai_analysis, dict) else {}
            recent.append(
                AlertContext(
                    event_id=row.id,
                    source=row.source or "unknown",
                    importance=row.importance or "medium",
                    parsed_data=parsed,
                    analysis=analysis,
                    timestamp=row.timestamp or window_start,
                    alert_hash=row.alert_hash,
                )
            )

    return recent


async def _compute_noise_reduction(
    current_context: AlertContext,
    recent_contexts: list[AlertContext],
    min_confidence: float = 0.65,
) -> tuple[NoiseReductionDecision, bool]:
    """
    计算当前告警的降噪上下文。

    Returns:
        (decision, is_root_cause): 降噪决策和是否为根因告警
    """
    if not recent_contexts:
        return _default_noise_decision(), False

    try:
        decision = analyze_noise_reduction(
            current_context,
            recent_contexts,
            window_minutes=5,
            min_confidence=min_confidence,
            suppress_derived=True,
        )
        is_root = decision.confidence >= min_confidence and decision.relation in ("root_cause", "derived")
        return decision, is_root
    except Exception as e:
        logger.warning(f"降噪分析失败: {e}")
        return _default_noise_decision(), False


def _apply_noise_metadata(analysis_result: dict[str, Any], decision: NoiseReductionDecision) -> dict[str, Any]:
    """
    将降噪元数据合并到 AI 分析结果中。
    """
    result = dict(analysis_result)
    result["_noise_reduction"] = {
        "relation": decision.relation,
        "root_cause_event_id": decision.root_cause_event_id,
        "confidence": round(decision.confidence, 4),
        "suppress_forward": decision.suppress_forward,
        "reason": decision.reason,
        "related_alert_count": decision.related_alert_count,
    }
    return result


def _persist_webhook_with_noise_context(
    webhook_event: WebhookEvent,
    current_time: datetime,
    decision: NoiseReductionDecision,
    analysis_result: dict[str, Any],
    is_root_cause: bool,
) -> None:
    """
    将降噪上下文元数据写入事件记录（存储在 ai_analysis 字段的 _noise_reduction 中）。
    同时更新 WebhookEvent 的 is_duplicate / duplicate_of 等字段。
    """
    # 合并降噪信息到分析结果
    enriched_result = _apply_noise_metadata(analysis_result, decision)

    # 写入 enriched 结果（供后续流程判断是否转发）
    webhook_event.ai_analysis = enriched_result
