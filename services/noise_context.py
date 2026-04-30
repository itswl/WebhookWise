"""
services/noise_context.py
===============================
告警智能降噪相关辅助函数。
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from db.session import session_scope
from models import WebhookEvent
from services.alert_noise_reduction import AlertContext, analyze_noise_reduction

logger = logging.getLogger(__name__)


# ── 告警上下文构建 ─────────────────────────────────────────────────────────────


def _default_noise_context() -> AlertContext:
    """降噪禁用时返回的默认上下文"""
    return AlertContext(
        relation="standalone",
        root_cause_event_id=None,
        confidence=0.0,
        suppress_forward=False,
        reason="降噪功能已禁用",
        related_alert_count=0,
        related_alert_ids=[],
    )


def _build_alert_context(current_event: WebhookEvent, current_time: datetime) -> AlertContext:
    """
    从当前事件构建 AlertContext，供 analyze_noise_reduction 使用。
    提取当前事件的字段用于关联分析。
    """
    if current_event.parsed_data is None:
        parsed = {}
    elif isinstance(current_event.parsed_data, dict):
        parsed = current_event.parsed_data
    else:
        parsed = {}

    importance = current_event.importance or "medium"
    current_hash = getattr(current_event, "alert_hash", None) or ""

    return AlertContext(
        alert_id=current_event.id,
        alert_hash=current_hash,
        importance=importance,
        source=current_event.source or "unknown",
        timestamp=current_time,
        parsed_data=parsed,
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
            ).filter(WebhookEvent.timestamp >= window_start, WebhookEvent.timestamp <= current_time)
        )
        rows = result.all()

        for row in rows:
            parsed = {}
            if row.parsed_data:
                parsed = row.parsed_data if isinstance(row.parsed_data, dict) else {}

            recent.append(
                AlertContext(
                    alert_id=row.id,
                    alert_hash=row.alert_hash or "",
                    importance=row.importance or "medium",
                    source=row.source or "unknown",
                    timestamp=row.timestamp or window_start,
                    parsed_data=parsed,
                )
            )

    return recent


async def _compute_noise_reduction(
    current_context: AlertContext,
    recent_contexts: list[AlertContext],
    min_confidence: float = 0.65,
) -> tuple[AlertContext, bool]:
    """
    计算当前告警的降噪上下文。

    Returns:
        (noise_context, is_root_cause): 降噪上下文和是否为根因告警
    """
    if not recent_contexts:
        return _default_noise_context(), False

    try:
        result = analyze_noise_reduction(current_context, recent_contexts)
        is_root = result.confidence >= min_confidence and result.relation in ("root_cause", "derived")
        return result, is_root
    except Exception as e:
        logger.warning(f"降噪分析失败: {e}")
        return _default_noise_context(), False


async def _apply_noise_metadata(analysis_result: dict, noise_context: AlertContext) -> dict:
    """
    将降噪元数据合并到 AI 分析结果中。
    """
    result = dict(analysis_result)
    result["_noise_reduction"] = {
        "relation": noise_context.relation,
        "root_cause_event_id": noise_context.root_cause_event_id,
        "confidence": round(noise_context.confidence, 4),
        "suppress_forward": noise_context.suppress_forward,
        "reason": noise_context.reason,
        "related_alert_count": noise_context.related_alert_count,
    }
    return result


def _persist_webhook_with_noise_context(
    webhook_event: WebhookEvent,
    current_time: datetime,
    noise_context: AlertContext,
    analysis_result: dict,
    is_root_cause: bool,
) -> None:
    """
    将降噪上下文元数据写入事件记录（存储在 ai_analysis 字段的 _noise_reduction 中）。
    同时更新 WebhookEvent 的 is_duplicate / duplicate_of 等字段。
    """
    # 合并降噪信息到分析结果
    enriched_result = _apply_noise_metadata(analysis_result, noise_context)

    # 如果是衍生告警且配置了 suppress_forward，更新事件标记
    if noise_context.suppress_forward:
        # 标记当前事件为衍生告警（由根因触发，不独立转发）
        logger.debug(f"告警 {webhook_event.id} 被标记为衍生告警，抑制转发")

    # 写入 enriched 结果（供后续流程判断是否转发）
    webhook_event.ai_analysis = enriched_result
