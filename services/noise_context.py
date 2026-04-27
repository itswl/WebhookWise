"""
services/noise_context.py
===============================
告警智能降噪相关辅助函数。
"""
import logging
from datetime import datetime, timedelta

from db.session import session_scope
from models import WebhookEvent
from services.alert_noise_reduction import AlertContext, analyze_noise_reduction

logger = logging.getLogger(__name__)


# ── 告警上下文构建 ─────────────────────────────────────────────────────────────

def _default_noise_context() -> AlertContext:
    """降噪禁用时返回的默认上下文"""
    return AlertContext(
        relation='standalone',
        root_cause_event_id=None,
        confidence=0.0,
        suppress_forward=False,
        reason='降噪功能已禁用',
        related_alert_count=0,
        related_alert_ids=[]
    )


def _build_alert_context(
    current_event: WebhookEvent,
    current_time: datetime
) -> AlertContext:
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

    importance = current_event.importance or 'medium'
    current_hash = getattr(current_event, 'alert_hash', None) or ''

    return AlertContext(
        alert_id=current_event.id,
        alert_hash=current_hash,
        importance=importance,
        source=current_event.source or 'unknown',
        timestamp=current_time,
        parsed_data=parsed
    )


def _load_recent_alert_contexts(
    current_hash: str,
    current_time: datetime,
    window_minutes: int = 5
) -> list[AlertContext]:
    """
    加载当前告警前后窗口内的相关告警上下文。
    用于根因分析的时间窗口关联。
    """
    recent: list[AlertContext] = []
    window_start = current_time - timedelta(minutes=window_minutes)

    with session_scope() as session:
        # 查找时间窗口内的其他告警（排除自身）
        events = session.query(WebhookEvent).filter(
            WebhookEvent.timestamp >= window_start,
            WebhookEvent.timestamp <= current_time,
            WebhookEvent.id != getattr(session, 'id', None)  # 已在 session 中时过滤自身
        ).all()

        for event in events:
            parsed = {}
            if event.parsed_data:
                parsed = event.parsed_data if isinstance(event.parsed_data, dict) else {}

            recent.append(AlertContext(
                alert_id=event.id,
                alert_hash=getattr(event, 'alert_hash', '') or '',
                importance=event.importance or 'medium',
                source=event.source or 'unknown',
                timestamp=event.timestamp or window_start,
                parsed_data=parsed
            ))

    return recent


def _compute_noise_reduction(
    current_context: AlertContext,
    recent_contexts: list[AlertContext],
    min_confidence: float = 0.65,
    use_dynamic_threshold: bool = False,
    session=None
) -> tuple[AlertContext, bool]:
    """
    计算当前告警的降噪上下文。

    Returns:
        (noise_context, is_root_cause): 降噪上下文和是否为根因告警
    """
    # 动态阈值功能已下线（AlertCorrelation 模型已废弃），始终使用固定阈值
    effective_threshold = min_confidence
    logger.debug(f"使用固定阈值: {effective_threshold:.4f}")

    if not recent_contexts:
        return _default_noise_context(), False

    try:
        result = analyze_noise_reduction(current_context, recent_contexts)
        is_root = (
            result.confidence >= effective_threshold
            and result.relation in ('root_cause', 'derived')
        )
        return result, is_root
    except Exception as e:
        logger.warning(f"降噪分析失败: {e}")
        return _default_noise_context(), False


def _apply_noise_metadata(
    analysis_result: dict,
    noise_context: AlertContext
) -> dict:
    """
    将降噪元数据合并到 AI 分析结果中。
    """
    result = dict(analysis_result)
    result['_noise_reduction'] = {
        'relation': noise_context.relation,
        'root_cause_event_id': noise_context.root_cause_event_id,
        'confidence': round(noise_context.confidence, 4),
        'suppress_forward': noise_context.suppress_forward,
        'reason': noise_context.reason,
        'related_alert_count': noise_context.related_alert_count,
    }
    return result


def _persist_webhook_with_noise_context(
    webhook_event: WebhookEvent,
    current_time: datetime,
    noise_context: AlertContext,
    analysis_result: dict,
    is_root_cause: bool
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
