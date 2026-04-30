"""分析决策模块，从 pipeline.py 提取。"""

import asyncio
import time as _time
from datetime import datetime

from api import AnalysisResolution
from core.config import Config
from core.logger import logger
from models import WebhookEvent
from services.ai_analyzer import analyze_webhook_with_ai
from services.ai_cache import get_cached_analysis, log_ai_usage
from services.dedup_strategy import check_duplicate_alert


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
            if seconds_since < Config.retry.RECENT_BEYOND_WINDOW_REUSE_SECONDS:
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
            f"窗口外历史记录 ID={last_beyond_window_event.id} 已超过复用窗口({Config.retry.RECENT_BEYOND_WINDOW_REUSE_SECONDS}s)，将尝试重新分析"
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
            Config.retry.REANALYZE_AFTER_TIME_WINDOW,
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
    """未获得处理锁时，通过 Redis Pub/Sub 等待其他 Worker 完成分析后复用结果。"""
    from core.redis_client import get_redis

    logger.info(f"[Lock] 告警正在由其他节点处理，Pub/Sub 等待中: hash={alert_hash[:16]}")

    redis = get_redis()
    channel = f"analysis_done:{alert_hash}"
    pubsub = redis.pubsub()

    try:
        await pubsub.subscribe(channel)

        # 关键：先订阅再检查缓存（避免竞态：结果在订阅前已写入）
        cached = await get_cached_analysis(alert_hash)
        if cached:
            logger.info(f"[Lock] 订阅前缓存已命中，直接复用: hash={alert_hash[:16]}")
            await log_ai_usage(
                route_type="reuse",
                alert_hash=alert_hash,
                source=webhook_full_data.get("source", ""),
            )
            return AnalysisResolution(cached, False, True, None, False, is_reused=True)

        # 等待 Pub/Sub 通知
        deadline = _time.monotonic() + Config.retry.PROCESSING_LOCK_WAIT_SECONDS  # 30s

        while _time.monotonic() < deadline:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break

            # get_message timeout 是轮询间隔，配合 wait_for 做真正的超时控制
            # 每 5 秒最多唤醒一次做 fallback 缓存检查
            wait_slice = min(remaining, 5.0)
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=wait_slice),
                    timeout=wait_slice + 1.0,  # 留 1s 余量防止内层永久阻塞
                )
            except asyncio.TimeoutError:
                message = None

            if message and message.get("type") == "message":
                cached = await get_cached_analysis(alert_hash)
                if cached:
                    logger.info(f"[Lock] Pub/Sub 通知命中，复用分析结果: hash={alert_hash[:16]}")
                    await log_ai_usage(
                        route_type="reuse",
                        alert_hash=alert_hash,
                        source=webhook_full_data.get("source", ""),
                    )
                    return AnalysisResolution(cached, False, True, None, False, is_reused=True)

            # 安全网：即使没收到消息，也定期检查缓存（防止 publish 丢失）
            cached = await get_cached_analysis(alert_hash)
            if cached:
                logger.info(f"[Lock] 定期检查缓存命中，复用分析结果: hash={alert_hash[:16]}")
                await log_ai_usage(
                    route_type="reuse",
                    alert_hash=alert_hash,
                    source=webhook_full_data.get("source", ""),
                )
                return AnalysisResolution(cached, False, True, None, False, is_reused=True)

    finally:
        # 清理订阅，防止连接泄漏
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception as exc:
            logger.debug("pubsub 关闭时异常（已忽略）: %s", exc)

    # ── Pub/Sub 等待超时 fallback：与原逻辑一致 ──
    logger.warning(f"[Lock] Pub/Sub 等待 {Config.retry.PROCESSING_LOCK_WAIT_SECONDS}s 超时，执行兜底分析")

    duplicate_check = await check_duplicate_alert(alert_hash, check_beyond_window=True)
    is_duplicate = duplicate_check.is_duplicate
    original_event = duplicate_check.original_event
    beyond_window = duplicate_check.beyond_window
    last_beyond_window_event = duplicate_check.last_beyond_window_event

    if last_beyond_window_event and last_beyond_window_event.created_at:
        seconds_since_created = (datetime.now() - last_beyond_window_event.created_at).total_seconds()
        if seconds_since_created < Config.retry.RECENT_BEYOND_WINDOW_REUSE_SECONDS:
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
            Config.retry.REANALYZE_AFTER_TIME_WINDOW,
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
