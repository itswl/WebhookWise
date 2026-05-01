"""转发决策与执行模块，从 pipeline.py 提取。"""

import asyncio
from datetime import datetime

from sqlalchemy import select, update

from api import ForwardDecision, NoiseReductionContext, WebhookRequestContext
from core.config import Config
from core.config_provider import policies
from core.logger import logger
from crud.webhook import record_failed_forward
from db.session import session_scope
from models import DeepAnalysis, ForwardRule, WebhookEvent
from services.forward import forward_to_openclaw, forward_to_remote


async def _refresh_original_event(original_id: int | None, fallback_event: WebhookEvent | None) -> WebhookEvent | None:
    if not original_id:
        return fallback_event

    try:
        async with session_scope() as session:
            latest = await session.get(WebhookEvent, original_id)
            return latest or fallback_event
    except Exception as e:
        logger.warning(f"重新查询原始告警失败: {e}")
        return fallback_event


async def _recently_notified(original_event: WebhookEvent | None, original_id: int | None, alert_type: str) -> bool:
    if not original_event or not original_event.last_notified_at:
        return False

    seconds_since_notify = (datetime.now() - original_event.last_notified_at).total_seconds()
    if seconds_since_notify < policies.retry.NOTIFICATION_COOLDOWN_SECONDS:
        logger.info(f"{alert_type}（原始 ID={original_id}），{seconds_since_notify:.1f}秒前已转发，跳过")
        return True

    return False


async def _resolve_alert_type_label(is_duplicate: bool, beyond_window: bool, is_periodic_reminder: bool) -> str:
    if is_periodic_reminder:
        return "周期性提醒"
    if is_duplicate:
        return "窗口内重复"
    if beyond_window:
        return "窗口外重复"
    return "新"


async def _decide_duplicate_forwarding(original_event: WebhookEvent | None, original_id: int | None) -> ForwardDecision:
    if await _recently_notified(original_event, original_id, "窗口内重复告警"):
        return ForwardDecision(False, f"窗口内重复告警（原始 ID={original_id}），刚刚已转发", False)

    if policies.retry.ENABLE_PERIODIC_REMINDER and original_event:
        last_notified = original_event.last_notified_at
        if last_notified:
            hours_since_notification = (datetime.now() - last_notified).total_seconds() / 3600
            if hours_since_notification >= policies.retry.REMINDER_INTERVAL_HOURS:
                logger.info(
                    f"触发周期性提醒: 原始ID={original_id}, 距上次通知{hours_since_notification:.1f}小时, 已重复{original_event.duplicate_count}次"
                )
                return ForwardDecision(True, None, True)
            return ForwardDecision(
                False, f"窗口内重复告警（原始 ID={original_id}），距上次通知仅{hours_since_notification:.1f}小时", False
            )

    if not policies.retry.FORWARD_DUPLICATE_ALERTS:
        return ForwardDecision(False, f"窗口内重复告警（原始 ID={original_id}），配置跳过转发", False)

    return ForwardDecision(True, None, False)


async def _match_forward_rules(importance: str, is_duplicate: bool, beyond_window: bool, source: str) -> list:
    try:
        async with session_scope() as session:
            result = await session.execute(
                select(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc())
            )
            rules = result.scalars().all()

            if not rules:
                return []

            matched = []
            for rule in rules:
                if rule.match_importance:
                    allowed = [x.strip().lower() for x in rule.match_importance.split(",")]
                    if importance.lower() not in allowed:
                        continue

                if rule.match_duplicate and rule.match_duplicate != "all":
                    if rule.match_duplicate == "new" and (is_duplicate or beyond_window):
                        continue
                    if rule.match_duplicate == "duplicate" and not is_duplicate:
                        continue
                    if rule.match_duplicate == "beyond_window" and not beyond_window:
                        continue

                if rule.match_source:
                    allowed_sources = [x.strip().lower() for x in rule.match_source.split(",")]
                    if source.lower() not in allowed_sources:
                        continue

                matched.append(rule.to_dict())

                if rule.stop_on_match:
                    break

            return matched
    except Exception as e:
        logger.warning(f"加载转发规则失败: {e}")
        return []


async def decide_forwarding(
    importance: str,
    is_duplicate: bool,
    beyond_window: bool,
    noise_context: NoiseReductionContext | None,
    original_event: WebhookEvent | None,
    original_id: int | None,
    source: str = "",
) -> ForwardDecision:
    """对外入口：做出转发决策。"""
    if noise_context and noise_context.suppress_forward:
        return ForwardDecision(
            False,
            f"智能降噪抑制转发: {noise_context.reason}",
            False,
        )

    matched_rules = await _match_forward_rules(importance, is_duplicate, beyond_window, source)

    if matched_rules:
        if is_duplicate:
            dup_decision = await _decide_duplicate_forwarding(original_event, original_id)
            if not dup_decision.should_forward:
                return ForwardDecision(False, dup_decision.skip_reason, False)
            return ForwardDecision(True, None, dup_decision.is_periodic_reminder, matched_rules)

        if beyond_window:
            if not policies.retry.FORWARD_AFTER_TIME_WINDOW:
                return ForwardDecision(False, "窗口外重复告警，配置不转发", False)
            if await _recently_notified(original_event, original_id, "窗口外重复告警"):
                return ForwardDecision(False, "近期已通知", False)
            return ForwardDecision(True, None, False, matched_rules)

        return ForwardDecision(True, None, False, matched_rules)

    if importance != "high":
        return ForwardDecision(False, f"重要性为 {importance}，非高风险事件不自动转发", False)

    if beyond_window:
        if not policies.retry.FORWARD_AFTER_TIME_WINDOW:
            return ForwardDecision(False, f"窗口外重复告警（原始 ID={original_id}），配置跳过转发", False)
        if await _recently_notified(original_event, original_id, "窗口外重复告警"):
            return ForwardDecision(False, f"窗口外重复告警（原始 ID={original_id}），刚刚已转发", False)
        return ForwardDecision(True, None, False)

    if is_duplicate:
        return await _decide_duplicate_forwarding(original_event, original_id)

    return ForwardDecision(True, None, False)


async def _update_last_notified(event_id: int) -> None:
    try:
        async with session_scope() as session:
            await session.execute(
                update(WebhookEvent).where(WebhookEvent.id == event_id).values(last_notified_at=datetime.now())
            )
            await session.commit()
            logger.info(f"已更新原始告警 {event_id} 的 last_notified_at")
    except Exception as e:
        logger.warning(f"更新 last_notified_at 失败: {e}")


async def execute_forwarding(
    forward_decision: ForwardDecision,
    request_context: WebhookRequestContext,
    analysis_result: dict,
    persisted,
    original_event: WebhookEvent | None = None,
) -> dict:
    """执行转发（基于规则或默认），返回 forward_result dict。"""
    save_result = persisted.save_result

    forward_result = {"status": "skipped", "reason": forward_decision.skip_reason}

    if not forward_decision.should_forward:
        logger.info(f"跳过自动转发: {forward_decision.skip_reason}")
        return forward_result

    is_duplicate = getattr(save_result, "is_duplicate", False)
    beyond_window = getattr(save_result, "beyond_window", False)

    alert_type = await _resolve_alert_type_label(is_duplicate, beyond_window, forward_decision.is_periodic_reminder)

    if forward_decision.matched_rules:
        # ── 阶段 1：构建并发 HTTP 任务列表 ──
        tasks: list[tuple[dict, asyncio.coroutines]] = []
        for rule in forward_decision.matched_rules:
            logger.info(f"准备规则转发: {rule['name']} -> {rule['target_type']}")
            if rule["target_type"] == "openclaw":
                coro = forward_to_openclaw(request_context.webhook_full_data, analysis_result)
            else:
                coro = forward_to_remote(
                    request_context.webhook_full_data,
                    analysis_result,
                    target_url=rule["target_url"],
                    is_periodic_reminder=forward_decision.is_periodic_reminder,
                )
            tasks.append((rule, coro))

        logger.info(f"并发转发 {len(tasks)} 个目标...")
        http_results = await asyncio.gather(
            *[coro for _, coro in tasks],
            return_exceptions=True,
        )

        # ── 阶段 2：串行处理结果（DB 操作） ──
        forward_results = []
        for (rule, _), http_result in zip(tasks, http_results, strict=True):
            if isinstance(http_result, BaseException):
                logger.error(f"规则 {rule['name']} 转发失败: {http_result}")
                err_result = {"status": "error", "rule_name": rule["name"], "message": str(http_result)}
                forward_results.append(err_result)
                try:
                    await record_failed_forward(
                        webhook_event_id=save_result.webhook_id,
                        forward_rule_id=rule.get("id"),
                        target_url=rule.get("target_url", ""),
                        target_type=rule.get("target_type", "webhook"),
                        failure_reason="exception",
                        error_message=str(http_result),
                        forward_data=request_context.webhook_full_data,
                    )
                except Exception as rec_err:
                    logger.warning(f"记录失败转发异常（不影响主流程）: {rec_err}")
                continue

            result = http_result
            result["rule_name"] = rule["name"]
            forward_results.append(result)

            # openclaw 类型：创建 DeepAnalysis 记录
            if rule["target_type"] == "openclaw" and result.get("_pending") and result.get("run_id"):
                try:
                    async with session_scope() as session:
                        deep_record = DeepAnalysis(
                            webhook_event_id=save_result.webhook_id,
                            engine="openclaw",
                            user_question="",
                            analysis_result={
                                "status": "pending",
                                "root_cause": "OpenClaw Agent 正在分析中，结果将自动更新...",
                                "impact": "分析已触发，预计几分钟内完成",
                                "recommendations": ["结果将自动更新，请稍后刷新页面"],
                                "confidence": 0,
                            },
                            openclaw_run_id=result.get("run_id", ""),
                            openclaw_session_key=result.get("session_key", ""),
                            status="pending",
                        )
                        session.add(deep_record)
                        await session.flush()
                        logger.info(f"转发分析记录已创建: id={deep_record.id}, run_id={result.get('run_id')}")
                except Exception as e:
                    logger.error(f"创建转发分析记录失败: {e}")

            # 非 openclaw 类型：失败时记录
            if result.get("status") != "success" and rule["target_type"] != "openclaw":
                try:
                    await record_failed_forward(
                        webhook_event_id=save_result.webhook_id,
                        forward_rule_id=rule.get("id"),
                        target_url=rule.get("target_url", ""),
                        target_type=rule.get("target_type", "webhook"),
                        failure_reason=result.get("status", "unknown"),
                        error_message=result.get("message") or result.get("response", ""),
                        forward_data=request_context.webhook_full_data,
                    )
                except Exception as rec_err:
                    logger.warning(f"记录失败转发异常（不影响主流程）: {rec_err}")

        forward_result = {"status": "success", "results": forward_results}
        if any(r.get("status") == "success" for r in forward_results) and original_event:
            await _update_last_notified(original_event.id)
    else:
        logger.info(f"开始自动转发高风险{alert_type}告警...")
        forward_result = await forward_to_remote(
            request_context.webhook_full_data,
            analysis_result,
            is_periodic_reminder=forward_decision.is_periodic_reminder,
        )
        if forward_result.get("status") == "success" and original_event:
            await _update_last_notified(original_event.id)
        elif forward_result.get("status") != "success":
            try:
                await record_failed_forward(
                    webhook_event_id=save_result.webhook_id,
                    forward_rule_id=None,
                    target_url=Config.ai.FEISHU_WEBHOOK_URL or "",
                    target_type="webhook",
                    failure_reason=forward_result.get("status", "unknown"),
                    error_message=forward_result.get("message") or forward_result.get("response", ""),
                    forward_data=request_context.webhook_full_data,
                )
            except Exception as rec_err:
                logger.warning(f"记录默认转发失败异常（不影响主流程）: {rec_err}")

    return forward_result
