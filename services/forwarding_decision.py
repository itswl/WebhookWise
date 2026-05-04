"""分析决策 + 转发决策相关辅助函数。"""

from datetime import datetime

from sqlalchemy import select

from core.config import Config, policies
from core.logger import logger
from db.session import session_scope
from models import ForwardRule, WebhookEvent

# ── 分析决策 helpers ────────────────────────────────────────────────────────────


async def _analyze_now(webhook_full_data: dict, message: str) -> tuple[dict, bool]:
    from services.ai_analyzer import analyze_webhook_with_ai

    result = await analyze_webhook_with_ai(webhook_full_data, skip_cache=True)
    return result, True


async def _resolve_duplicate_analysis(original_event: WebhookEvent, webhook_full_data: dict) -> tuple[dict, bool]:
    if original_event.ai_analysis:
        return original_event.ai_analysis, False
    # 兜底：复用原始告警的 importance 作为摘要
    return {
        "summary": f"重复告警（参考 #{original_event.id}）",
        "importance": original_event.importance or "medium",
    }, False


async def _resolve_beyond_window_analysis(
    original_event: WebhookEvent | None, webhook_full_data: dict, reanalyze: bool = True
) -> tuple[dict, bool]:
    if reanalyze and policies.retry.REANALYZE_AFTER_TIME_WINDOW:
        return await _analyze_now(webhook_full_data, "窗口外重复告警重新分析")
    if original_event and original_event.ai_analysis:
        return original_event.ai_analysis, False
    return {"summary": "窗口外重复告警（无历史分析结果）", "importance": "medium"}, False


async def _refresh_original_event(original_id: int, fallback_event: WebhookEvent | None) -> WebhookEvent | None:
    try:
        async with session_scope() as session:
            result = await session.execute(select(WebhookEvent).filter_by(id=original_id))
            return result.scalars().first()
    except Exception:
        return fallback_event


async def _resolve_analysis_with_lock(
    alert_hash: str, webhook_full_data: dict
) -> tuple[dict, bool, bool, WebhookEvent | None]:
    from datetime import datetime

    from models import WebhookEvent

    async with session_scope() as session:
        current_time = datetime.now()
        # 查询同 hash 最近一条记录
        original_event = result = await session.execute(
            select(WebhookEvent).filter(WebhookEvent.alert_hash == alert_hash).order_by(WebhookEvent.id.desc())
        )
        original_event = result.scalars().first()

        is_duplicate = original_event is not None
        _original_id = original_event.id if original_event else None

        # 时间窗口判断
        time_window_hours = policies.retry.DUPLICATE_ALERT_TIME_WINDOW
        beyond_window = False
        if original_event and original_event.timestamp:
            hours_elapsed = (current_time - original_event.timestamp).total_seconds() / 3600
            beyond_window = hours_elapsed >= time_window_hours

        reanalyze = policies.retry.REANALYZE_AFTER_TIME_WINDOW

        # 分析决策
        if not is_duplicate:
            analysis_result = await _analyze_now(webhook_full_data, "首次告警")
            reanalyzed = True
        elif not beyond_window:
            analysis_result, reanalyzed = await _resolve_duplicate_analysis(original_event, webhook_full_data)
        else:
            analysis_result, reanalyzed = await _resolve_beyond_window_analysis(
                original_event, webhook_full_data, reanalyze
            )

        return analysis_result, reanalyzed, is_duplicate, original_event


async def _resolve_analysis_without_lock(
    alert_hash: str, webhook_full_data: dict
) -> tuple[dict, bool, bool, WebhookEvent | None]:
    import asyncio

    wait_time = 0
    while wait_time < Config.retry.PROCESSING_LOCK_WAIT_SECONDS:
        await asyncio.sleep(0.5)
        wait_time += 0.5
        refreshed = await _refresh_original_event(None, None)
        if refreshed and refreshed.ai_analysis:
            return refreshed.ai_analysis, False, True, refreshed

    # 等待超时，降级
    logger.warning(f"等待锁超时，alert_hash={alert_hash}")
    return {"summary": "正在被其他请求处理中，请稍后刷新", "importance": "medium"}, False, True, None


# ── 转发决策 helpers ────────────────────────────────────────────────────────────


async def _recently_notified(original_event: WebhookEvent | None, original_id: int | None, alert_type: str) -> bool:
    if original_event and original_event.last_notified_at:
        elapsed = (datetime.now() - original_event.last_notified_at).total_seconds()
        if elapsed < policies.retry.NOTIFICATION_COOLDOWN_SECONDS:
            logger.debug(f"{alert_type} {original_id} 刚刚已通知（{elapsed:.0f}s 前），跳过")
            return True
    return False


async def _resolve_alert_type_label(is_duplicate: bool, beyond_window: bool, is_periodic_reminder: bool) -> str:
    if is_periodic_reminder:
        return "周期性重复提醒"
    if is_duplicate and beyond_window:
        return "窗口外重复"
    if is_duplicate:
        return "窗口内重复"
    return "新告警"


async def _decide_duplicate_forwarding(
    original_event: WebhookEvent | None, original_id: int | None, noise_context, importance: str
) -> tuple[bool, str | None]:
    # 衍生告警默认不转发（除非单独配置）
    if noise_context and noise_context.suppress_forward and policies.ai.SUPPRESS_DERIVED_ALERT_FORWARD:
        return False, f"抑制衍生告警（参考 #{noise_context.root_cause_event_id}）"

    # 检查是否在冷却期内
    if await _recently_notified(original_event, original_id, "重复告警"):
        return False, "重复告警，冷却期内"

    # 重要性过滤
    if importance != "high":
        return False, f"重要性为 {importance}，非高风险事件不自动转发"

    return True, None


async def _match_forward_rules(
    importance: str, is_duplicate: bool, beyond_window: bool, source: str
) -> list[ForwardRule]:
    async with session_scope() as session:
        result = await session.execute(
            select(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc())
        )
        rules = result.scalars().all()

        matched = []
        for rule in rules:
            # 重要性匹配
            if rule.match_importance:
                importances = [x.strip() for x in rule.match_importance.split(",")]
                if importance not in importances:
                    continue

            # 重复类型匹配
            if rule.match_duplicate not in ("all",):
                if (
                    is_duplicate
                    and not beyond_window
                    and rule.match_duplicate not in ("duplicate", "beyond_window", "all")
                ):
                    continue
                if is_duplicate and beyond_window and rule.match_duplicate not in ("beyond_window", "all"):
                    continue
                if not is_duplicate and rule.match_duplicate not in ("new", "all"):
                    continue

            # 来源匹配
            if rule.match_source:
                sources = [x.strip() for x in rule.match_source.split(",")]
                if source not in sources:
                    continue

            matched.append(rule)

            # 匹配后停止
            if rule.stop_on_match:
                break

        return matched


async def _decide_forwarding(
    noise_context,
    importance: str,
    is_duplicate: bool,
    beyond_window: bool,
    original_event: WebhookEvent | None,
    original_id: int | None,
    matched_rules: list[ForwardRule],
    is_periodic_reminder: bool = False,
) -> tuple[bool, str | None, bool, list[ForwardRule]]:
    # 噪音降噪抑制
    if noise_context and noise_context.suppress_forward and policies.ai.SUPPRESS_DERIVED_ALERT_FORWARD:
        return False, f"抑制衍生告警（根因 #{noise_context.root_cause_event_id}）", False, []

    # 无匹配规则 → 降级到原有逻辑
    if not matched_rules:
        if importance != "high":
            return False, f"重要性为 {importance}，非高风险事件不自动转发", False, []
        if beyond_window:
            if not policies.retry.FORWARD_AFTER_TIME_WINDOW:
                return False, f"窗口外重复告警（原始 ID={original_id}），配置跳过转发", False, []
            if await _recently_notified(original_event, original_id, "窗口外重复告警"):
                return False, f"窗口外重复告警（原始 ID={original_id}），刚刚已转发", False, []
            return True, None, False
        if is_duplicate:
            should_forward, reason = await _decide_duplicate_forwarding(
                original_event, original_id, noise_context, importance
            )
            return should_forward, reason, False, []
        return True, None, False, []

    # 有匹配规则 → 检查是否触发 periodic reminder
    if (
        matched_rules
        and is_duplicate
        and not beyond_window
        and await _recently_notified(original_event, original_id, "重复告警")
    ):
        return False, "重复告警，冷却期内", False, matched_rules

    return True, None, is_periodic_reminder, matched_rules


async def _update_last_notified(event_id: int) -> None:
    try:
        async with session_scope() as session:
            result = await session.execute(select(WebhookEvent).filter_by(id=event_id))
            event = result.scalars().first()
            if event:
                event.last_notified_at = datetime.now()
                await session.commit()
    except Exception as e:
        logger.warning(f"更新 last_notified_at 失败: {e}")
