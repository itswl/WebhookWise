"""分析决策模块，从 pipeline.py 提取。"""

import asyncio
import time as _time
from datetime import datetime

from api import AnalysisResolution
from core.config import Config
from core.logger import logger
from crud.webhook import check_duplicate_alert
from models import WebhookEvent
from services.ai_analyzer import analyze_webhook_with_ai, log_ai_usage


async def _analyze_now(webhook_full_data: dict, message: str) -> tuple[dict, bool]:
    logger.info(message)
    return await analyze_webhook_with_ai(webhook_full_data), True


async def _resolve_duplicate_analysis(
    original_event: WebhookEvent, last_beyond_window_event: WebhookEvent | None, webhook_full_data: dict
) -> tuple[dict, bool]:
    if last_beyond_window_event and last_beyond_window_event.ai_analysis:
        logger.info(f"检测到窗口内重复，复用本窗口内最新分析结果 (ID={last_beyond_window_event.id})")
        await log_ai_usage(
            route_type="reuse",
            alert_hash=last_beyond_window_event.alert_hash or "",
            source=last_beyond_window_event.source or "",
        )
        return last_beyond_window_event.ai_analysis, False

    if original_event.ai_analysis:
        logger.info(f"复用原始告警 ID={original_event.id} 的分析结果")
        await log_ai_usage(
            route_type="reuse", alert_hash=original_event.alert_hash or "", source=original_event.source or ""
        )
        return original_event.ai_analysis, False

    return await _analyze_now(webhook_full_data, f"原始告警 ID={original_event.id} 缺少AI分析，重新分析")


async def _resolve_beyond_window_analysis(
    original_event: WebhookEvent | None,
    last_beyond_window_event: WebhookEvent | None,
    webhook_full_data: dict,
    allow_reanalyze: bool,
    prefer_recent_beyond_window: bool,
) -> tuple[dict, bool]:
    if prefer_recent_beyond_window and last_beyond_window_event:
        is_recent = False
        if last_beyond_window_event.created_at:
            seconds_since = (datetime.now() - last_beyond_window_event.created_at).total_seconds()
            if seconds_since < Config.RECENT_BEYOND_WINDOW_REUSE_SECONDS:
                is_recent = True

        if is_recent:
            logger.info(f"窗口外历史告警，发现其他worker刚完成分析(ID={last_beyond_window_event.id})，复用结果")
            await log_ai_usage(
                route_type="reuse",
                alert_hash=last_beyond_window_event.alert_hash or "",
                source=last_beyond_window_event.source or "",
            )
            return last_beyond_window_event.ai_analysis or {}, False

        logger.debug(
            f"窗口外历史记录 ID={last_beyond_window_event.id} 已超过复用窗口({Config.RECENT_BEYOND_WINDOW_REUSE_SECONDS}s)，将尝试重新分析"
        )

    if original_event and not allow_reanalyze:
        logger.info(f"窗口外历史告警(ID={original_event.id})，复用历史分析结果")
        await log_ai_usage(
            route_type="reuse", alert_hash=original_event.alert_hash or "", source=original_event.source or ""
        )
        return original_event.ai_analysis or {}, False

    if original_event:
        return await _analyze_now(webhook_full_data, f"窗口外历史告警(ID={original_event.id})，重新分析")

    return await _analyze_now(webhook_full_data, "窗口外历史告警缺少原始上下文，重新分析")


async def _resolve_analysis_with_lock(alert_hash: str, webhook_full_data: dict) -> AnalysisResolution:
    duplicate_check = await check_duplicate_alert(alert_hash, check_beyond_window=True)
    is_duplicate = duplicate_check.is_duplicate
    original_event = duplicate_check.original_event
    beyond_window = duplicate_check.beyond_window
    last_beyond_window_event = duplicate_check.last_beyond_window_event

    if beyond_window and original_event:
        analysis_result, reanalyzed = await _resolve_beyond_window_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data,
            Config.REANALYZE_AFTER_TIME_WINDOW,
            prefer_recent_beyond_window=False,
        )
    elif is_duplicate and original_event:
        analysis_result, reanalyzed = await _resolve_duplicate_analysis(
            original_event, last_beyond_window_event, webhook_full_data
        )
    else:
        analysis_result, reanalyzed = await _analyze_now(webhook_full_data, "新告警，开始 AI 分析...")

    return AnalysisResolution(analysis_result, reanalyzed, is_duplicate, original_event, beyond_window)


async def _resolve_analysis_without_lock(alert_hash: str, webhook_full_data: dict) -> AnalysisResolution:
    """未获得处理锁时，轮询等待其他 Worker 完成分析后复用结果。"""
    logger.info(f"[Lock] 告警正在由其他节点处理，轮询等待中: hash={alert_hash[:16]}")

    deadline = _time.monotonic() + Config.PROCESSING_LOCK_WAIT_SECONDS
    poll_interval = Config.PROCESSING_LOCK_POLL_INTERVAL_MS / 1000.0

    while _time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)

        duplicate_check = await check_duplicate_alert(alert_hash, check_beyond_window=True)
        original_event = duplicate_check.original_event
        last_beyond_window_event = duplicate_check.last_beyond_window_event

        # 窗口内重复：原始事件的 ai_analysis 已填充 → 分析完成
        if duplicate_check.is_duplicate and original_event and original_event.ai_analysis:
            logger.info(f"[Lock] 其他 Worker 已完成分析，复用结果: original_id={original_event.id}")
            analysis_result, reanalyzed = await _resolve_duplicate_analysis(
                original_event, last_beyond_window_event, webhook_full_data
            )
            return AnalysisResolution(analysis_result, reanalyzed, True, original_event, False)

        # 窗口外重复：last_beyond_window_event 已有分析结果 → 复用
        if last_beyond_window_event and last_beyond_window_event.ai_analysis and last_beyond_window_event.created_at:
            seconds_since = (datetime.now() - last_beyond_window_event.created_at).total_seconds()
            if seconds_since < Config.RECENT_BEYOND_WINDOW_REUSE_SECONDS:
                logger.info(
                    f"[Lock] 其他 worker 刚完成窗口外分析"
                    f"(ID={last_beyond_window_event.id}, {seconds_since:.1f}s前)，复用结果"
                )
                await log_ai_usage(
                    route_type="reuse",
                    alert_hash=last_beyond_window_event.alert_hash or "",
                    source=last_beyond_window_event.source or "",
                )
                return AnalysisResolution(last_beyond_window_event.ai_analysis, False, True, original_event, False)

    # ── 轮询超时 fallback：与原逻辑一致 ──
    logger.warning(f"[Lock] 轮询等待 {Config.PROCESSING_LOCK_WAIT_SECONDS}s 超时，执行兜底分析")

    duplicate_check = await check_duplicate_alert(alert_hash, check_beyond_window=True)
    is_duplicate = duplicate_check.is_duplicate
    original_event = duplicate_check.original_event
    beyond_window = duplicate_check.beyond_window
    last_beyond_window_event = duplicate_check.last_beyond_window_event

    if last_beyond_window_event and last_beyond_window_event.created_at:
        seconds_since_created = (datetime.now() - last_beyond_window_event.created_at).total_seconds()
        if seconds_since_created < Config.RECENT_BEYOND_WINDOW_REUSE_SECONDS:
            logger.info(
                f"检测到其他 worker 刚处理完窗口外重复(ID={last_beyond_window_event.id}, {seconds_since_created:.1f}秒前)，复用结果"
            )
            await log_ai_usage(
                route_type="reuse",
                alert_hash=last_beyond_window_event.alert_hash or "",
                source=last_beyond_window_event.source or "",
            )
            analysis_result = last_beyond_window_event.ai_analysis or {}
            return AnalysisResolution(analysis_result, False, True, original_event, False)

    if beyond_window and original_event:
        analysis_result, reanalyzed = await _resolve_beyond_window_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data,
            Config.REANALYZE_AFTER_TIME_WINDOW,
            prefer_recent_beyond_window=True,
        )
    elif is_duplicate and original_event:
        analysis_result, reanalyzed = await _resolve_duplicate_analysis(
            original_event, last_beyond_window_event, webhook_full_data
        )
    else:
        analysis_result, reanalyzed = await _analyze_now(webhook_full_data, "未找到已处理结果，重新处理...")

    return AnalysisResolution(analysis_result, reanalyzed, is_duplicate, original_event, beyond_window)


async def resolve_analysis(alert_hash: str, webhook_full_data: dict, got_lock: bool) -> AnalysisResolution:
    """对外入口：根据是否获取锁来决定分析路径。"""
    if got_lock:
        return await _resolve_analysis_with_lock(alert_hash, webhook_full_data)
    return await _resolve_analysis_without_lock(alert_hash, webhook_full_data)
