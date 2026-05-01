"""告警判重策略层 — 从 crud/webhook.py 提取的判重逻辑。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config_provider import policies
from core.logger import logger
from crud.webhook import query_last_beyond_window_event, query_latest_original_event
from db.session import session_scope
from models import WebhookEvent

AnalysisResult = dict[str, Any]


@dataclass(frozen=True)
class DuplicateCheckResult:
    is_duplicate: bool
    original_event: WebhookEvent | None
    beyond_window: bool
    last_beyond_window_event: WebhookEvent | None


async def _find_recent_window_event(
    session: AsyncSession, alert_hash: str, time_threshold: datetime
) -> WebhookEvent | None:
    stmt = (
        select(WebhookEvent)
        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.timestamp >= time_threshold)
        .order_by(WebhookEvent.timestamp.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().first()


def _resolve_window_start(original_ref: WebhookEvent, last_beyond_window: WebhookEvent | None) -> tuple[datetime, int]:
    if last_beyond_window:
        logger.debug(f"找到窗口外记录作为起点: ID={last_beyond_window.id}, 时间={last_beyond_window.timestamp}")
        return last_beyond_window.timestamp, last_beyond_window.id

    logger.debug(f"使用原始告警作为起点: ID={original_ref.id}, 时间={original_ref.timestamp}")
    return original_ref.timestamp, original_ref.id


async def _resolve_original_reference(session: AsyncSession, any_event: WebhookEvent) -> WebhookEvent:
    original_id = any_event.duplicate_of if any_event.is_duplicate else any_event.id
    if not original_id:
        return any_event
    ref = await session.get(WebhookEvent, original_id)
    return ref or any_event


async def check_duplicate_alert(
    alert_hash: str,
    time_window_hours: int | None = None,
    session: AsyncSession | None = None,
    check_beyond_window: bool = False,
) -> DuplicateCheckResult:
    """
    检查是否存在重复告警

    Args:
        alert_hash: 告警哈希值
        time_window_hours: 时间窗口（小时）
        session: 数据库会话（如果提供，使用现有事务；否则创建新会话）
        check_beyond_window: 是否检查时间窗口外的历史告警

    Returns:
        DuplicateCheckResult
    """
    if not alert_hash:
        return DuplicateCheckResult(False, None, False, None)

    if time_window_hours is None:
        time_window_hours = policies.retry.DUPLICATE_ALERT_TIME_WINDOW

    if session is not None:
        # 使用调用方提供的 session，不管理其生命周期
        return await _do_check_duplicate(session, alert_hash, time_window_hours, check_beyond_window)

    # 无外部 session 时，通过 session_scope 管理生命周期（保证异常时回滚+关闭）
    async with session_scope() as scoped_session:
        return await _do_check_duplicate(scoped_session, alert_hash, time_window_hours, check_beyond_window)


async def _do_check_duplicate(
    session: AsyncSession, alert_hash: str, time_window_hours: int, check_beyond_window: bool
) -> DuplicateCheckResult:
    """内部实现：在给定 session 上执行重复检查逻辑。"""
    now = datetime.now()

    try:
        time_threshold = now - timedelta(hours=time_window_hours)

        # 先查窗口内最新记录，保证同一时间窗口内只产生一条"原始上下文"。
        # 这样在并发写入时，后续请求可以稳定复用同一条分析结果。
        any_event = await _find_recent_window_event(session, alert_hash, time_threshold)

        if any_event:
            original_ref = await _resolve_original_reference(session, any_event)
            original_id = original_ref.id
            last_beyond_window = await query_last_beyond_window_event(session, alert_hash)

            # 窗口起点策略：优先 recent beyond_window，其次原始告警。
            window_start, window_start_id = _resolve_window_start(original_ref, last_beyond_window)

            time_diff_hours = (now - window_start).total_seconds() / 3600
            is_within_window = time_diff_hours <= time_window_hours

            if is_within_window:
                logger.info(
                    f"检测到窗口内重复: hash={alert_hash}, 最近记录ID={any_event.id}, "
                    f"原始告警ID={original_id}, 窗口起点ID={window_start_id}, "
                    f"距窗口起点={time_diff_hours:.1f}小时"
                )
                return DuplicateCheckResult(True, original_ref, False, last_beyond_window)

            logger.info(
                f"检测到窗口外重复: hash={alert_hash}, 最近记录ID={any_event.id}, "
                f"原始告警ID={original_id}, 窗口起点ID={window_start_id}, "
                f"距窗口起点={time_diff_hours:.1f}小时"
            )
            return DuplicateCheckResult(True, original_ref, True, last_beyond_window)

        if check_beyond_window:
            # 并发场景下，recent beyond_window 用于判断是否可直接复用他 worker 的结果。
            last_beyond_window = await query_last_beyond_window_event(session, alert_hash)
            history_event = await query_latest_original_event(session, alert_hash)

            if history_event:
                time_diff = (now - history_event.timestamp).total_seconds() / 3600
                logger.info(
                    f"窗口外发现历史告警: hash={alert_hash}, 原始告警ID={history_event.id}, 时间差={time_diff:.1f}小时"
                )
                # 返回历史原始事件与 recent beyond_window，交给上层做"复用或重算"决策。
                return DuplicateCheckResult(False, history_event, True, last_beyond_window)

        return DuplicateCheckResult(False, None, False, None)

    except Exception as e:
        logger.error(f"检查重复告警失败: {e!s}")
        return DuplicateCheckResult(False, None, False, None)


def _resolve_analysis_for_duplicate(
    ai_analysis: AnalysisResult | None, original: WebhookEvent, reanalyzed: bool
) -> tuple[AnalysisResult, str | None]:
    if ai_analysis:
        final_analysis = ai_analysis
        final_importance = ai_analysis.get("importance")
    elif original.ai_analysis:
        final_analysis = original.ai_analysis
        final_importance = original.importance
    else:
        final_analysis = {}
        final_importance = None

    if ai_analysis and reanalyzed and (not original.ai_analysis or not original.ai_analysis.get("summary")):
        logger.info(f"更新原始告警 ID={original.id} 的AI分析结果（之前缺失）")
        original.ai_analysis = ai_analysis
        original.importance = ai_analysis.get("importance")

    return final_analysis, final_importance
