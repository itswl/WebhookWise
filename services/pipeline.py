"""Webhook 处理主管线 — 纯协调层。
集成解析、分析决策、降噪计算与转发执行。
"""

import asyncio
import contextlib
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
import orjson
import sqlalchemy.exc
from sqlalchemy import select, update

from adapters.ecosystem_adapters import normalize_webhook_event
from core.compression import decompress_payload_async
from core.distributed_lock import processing_lock
from core.log_context import clear_log_context, set_log_context
from core.logger import logger
from core.metrics import (
    WEBHOOK_PROCESSING_DURATION_SECONDS,
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    WEBHOOK_RUNNING_TASKS,
    sanitize_source,
)
from core.trace import generate_trace_id, set_trace_id
from db.session import session_scope
from models import DeepAnalysis, ForwardRule, WebhookEvent
from services.ai_analyzer import analyze_webhook_with_ai, get_cached_analysis, log_ai_usage
from services.alert_noise_reduction import AlertContext, analyze_noise_reduction
from services.forward import forward_to_openclaw, forward_to_remote, record_failed_forward
from services.types import (
    AnalysisResolution,
    ForwardDecision,
    NoiseReductionContext,
    WebhookRequestContext,
)
from services.webhook_orchestrator import save_webhook_data

try:
    from openai import AuthenticationError as _OpenAIAuthenticationError
    from openai import BadRequestError as _OpenAIBadRequestError
    from openai import PermissionDeniedError as _OpenAIPermissionDeniedError
    from openai import UnprocessableEntityError as _OpenAIUnprocessableEntityError
except Exception:
    _OpenAIAuthenticationError = _OpenAIBadRequestError = _OpenAIPermissionDeniedError = _OpenAIUnprocessableEntityError = None

try:
    from asyncpg.exceptions import QueryCanceledError as _QueryCanceledError
except ImportError:
    _QueryCanceledError = None

_NON_RETRYABLE_ERRORS = (ValueError, KeyError, TypeError, orjson.JSONDecodeError, UnicodeDecodeError)
_MAX_RETRIES = 5
_running_tasks: set[asyncio.Task] = set()


# ── 核心辅助 ──────────────────────────────────────────────────────────────────


async def _load_event_payload(event: WebhookEvent) -> tuple[dict | None, str]:
    """从数据库记录中加载并解压 payload"""
    raw_text = await decompress_payload_async(event.raw_payload) or ""
    parsed_data = event.parsed_data
    if parsed_data is None and raw_text:
        try:
            parsed_data = orjson.loads(raw_text)
        except Exception:
            parsed_data = None
    return parsed_data, raw_text


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否可重试"""
    # 检查 OpenAI 报错
    visited = set()
    curr = exc
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        name = type(curr).__name__
        if name in {"BadRequestError", "UnprocessableEntityError", "PermissionDeniedError", "AuthenticationError"}:
            return False
        msg = str(curr).lower()
        if any(k in msg for k in ["context_length", "content_policy", "content filter"]):
            return False
        curr = curr.__cause__ or curr.__context__

    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, ConnectionError, OSError)):
        return True
    if _QueryCanceledError and isinstance(exc, _QueryCanceledError):
        return True
    if isinstance(exc, sqlalchemy.exc.OperationalError):
        return True
    return not isinstance(exc, _NON_RETRYABLE_ERRORS)


async def _send_dead_letter_alert(event_id: int, retry_count: int, error: Exception) -> None:
    """发送死信队列告警"""
    try:
        from core.config import Config
        from core.http_client import get_http_client
        url = Config.ai.FORWARD_URL
        if not url or "feishu.cn" not in url:
            return
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "🚨 Dead Letter 告警"}, "template": "red"},
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**event_id**: {event_id}\n**重试**: {retry_count}\n**详情**: {str(error)[:200]}"
                        }
                    }
                ]
            }
        }
        await get_http_client().post(url, json=card, timeout=10)
    except Exception as e:
        logger.warning("[Pipeline] 发送死信告警失败: %s", e)


# ── 阶段逻辑 ──────────────────────────────────────────────────────────────────


def _parse_request(
    client_ip: str, headers: dict, payload: dict, raw_body: bytes, source: str | None, ts: str | None
) -> WebhookRequestContext:
    src = source or headers.get("x-webhook-source", "unknown")
    if not payload and raw_body:
        payload = orjson.loads(raw_body)
    norm = normalize_webhook_event(payload, src)
    return WebhookRequestContext(
        client_ip=client_ip, source=norm.source, payload=raw_body,
        parsed_data=norm.data,
        webhook_full_data={
            "body": payload, "headers": headers, "parsed_data": norm.data,
            "source": norm.source, "timestamp": ts
        },
        headers=headers
    )


async def _resolve_analysis(alert_hash: str, full_data: dict, got_lock: bool) -> AnalysisResolution:
    from core.config import Config
    if not got_lock:
        # 锁竞争逻辑：Pub/Sub 等待
        from core.redis_client import get_redis
        redis = get_redis()
        channel = f"analysis_done:{alert_hash}"
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            cached = await get_cached_analysis(alert_hash)
            if cached:
                logger.debug("[Pipeline] 锁竞争立即命中缓存 hash=%s...", alert_hash[:12])
                await log_ai_usage(
                    route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", "")
                )
                return AnalysisResolution(cached, False, True, None, False, is_reused=True)
            deadline = time.monotonic() + Config.retry.PROCESSING_LOCK_WAIT_SECONDS
            while time.monotonic() < deadline:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0), timeout=6.0
                    )
                cached = await get_cached_analysis(alert_hash)
                if cached:
                    logger.debug("[Pipeline] 锁竞争 pub/sub 等待后命中缓存 hash=%s...", alert_hash[:12])
                    await log_ai_usage(
                        route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", "")
                    )
                    return AnalysisResolution(cached, False, True, None, False, is_reused=True)
            logger.debug("[Pipeline] 锁竞争等待超时，降级为独立分析 hash=%s...", alert_hash[:12])
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    # 持锁或等待超时：正常检查/分析
    async with session_scope() as session:
        check = await WebhookEvent.check_duplicate(alert_hash, session=session, time_window_hours=Config.retry.DUPLICATE_ALERT_TIME_WINDOW)
    orig, last_beyond = check.original_event, check.last_beyond_window_event

    if check.beyond_window and orig:
        if last_beyond and last_beyond.created_at and (
            datetime.now() - last_beyond.created_at
        ).total_seconds() < Config.retry.RECENT_BEYOND_WINDOW_REUSE_SECONDS and not (last_beyond.ai_analysis or {}).get("_degraded"):
            logger.debug("[Pipeline] 窗口外复用最近 beyond_window 事件分析 orig_id=%s hash=%s...", orig.id, alert_hash[:12])
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution({**(last_beyond.ai_analysis or {}), "_route_type": "db_reuse"}, False, True, orig, True)
        if not Config.retry.REANALYZE_AFTER_TIME_WINDOW and not (orig.ai_analysis or {}).get("_degraded"):
            logger.debug("[Pipeline] 窗口外复用原始事件分析 orig_id=%s hash=%s...", orig.id, alert_hash[:12])
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution({**(orig.ai_analysis or {}), "_route_type": "db_reuse"}, False, True, orig, True)
        logger.debug("[Pipeline] 窗口外重新分析 orig_id=%s reason=%s hash=%s...",
                     orig.id, "reanalyze_enabled" if Config.retry.REANALYZE_AFTER_TIME_WINDOW else "prev_degraded", alert_hash[:12])
        res, rean = await analyze_webhook_with_ai(full_data), True
    elif check.is_duplicate and orig:
        target = last_beyond if last_beyond and last_beyond.ai_analysis else orig
        if target.ai_analysis and not target.ai_analysis.get("_degraded"):
            logger.debug("[Pipeline] 窗口内复用原始事件分析 orig_id=%s hash=%s...", orig.id, alert_hash[:12])
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution({**target.ai_analysis, "_route_type": "db_reuse"}, False, True, orig, False)
        logger.debug("[Pipeline] 窗口内重新分析 orig_id=%s reason=prev_degraded hash=%s...", orig.id, alert_hash[:12])
        res, rean = await analyze_webhook_with_ai(full_data), True
    else:
        logger.debug("[Pipeline] 新事件，发起 AI 分析 hash=%s...", alert_hash[:12])
        res, rean = await analyze_webhook_with_ai(full_data), True

    return AnalysisResolution(res, rean, check.is_duplicate, orig, check.beyond_window)


async def _compute_noise(alert_hash: str, source: str, parsed: dict, analysis: dict) -> NoiseReductionContext:
    from core.config import Config
    if not Config.ai.ENABLE_ALERT_NOISE_REDUCTION:
        return NoiseReductionContext("standalone", None, 0.0, False, "智能降噪未启用", 0, [])
    now = datetime.now()
    window = max(1, Config.ai.NOISE_REDUCTION_WINDOW_MINUTES)
    try:
        async with session_scope() as session:
            stmt = select(WebhookEvent).filter(
                WebhookEvent.timestamp >= now - timedelta(minutes=window),
                WebhookEvent.timestamp <= now
            ).order_by(WebhookEvent.timestamp.desc()).limit(100)
            res = await session.execute(stmt)
            recent = [
                AlertContext(
                    e.id, e.source, e.importance, e.parsed_data or {},
                    e.ai_analysis or {}, e.timestamp or now, e.alert_hash
                )
                for e in res.scalars().all() if e.alert_hash != alert_hash
            ]
    except Exception:
        recent = []
    curr = AlertContext(None, source, analysis.get("importance", "medium"), parsed, analysis, now, alert_hash)
    dec = analyze_noise_reduction(
        curr, recent, window_minutes=window, min_confidence=Config.ai.ROOT_CAUSE_MIN_CONFIDENCE,
        suppress_derived=Config.ai.SUPPRESS_DERIVED_ALERT_FORWARD
    )
    if dec.suppress_forward:
        logger.info("[Noise] 抑制转发 relation=%s root_cause_id=%s confidence=%.2f reason=%s",
                    dec.relation, dec.root_cause_event_id, dec.confidence, dec.reason)
    elif dec.relation != "standalone":
        logger.debug("[Noise] 关联但不抑制 relation=%s root_cause_id=%s confidence=%.2f",
                     dec.relation, dec.root_cause_event_id, dec.confidence)
    return NoiseReductionContext(
        dec.relation, dec.root_cause_event_id, dec.confidence,
        dec.suppress_forward, dec.reason, dec.related_alert_count, dec.related_alert_ids
    )


async def _decide_forwarding(
    importance: str, is_duplicate: bool, beyond_window: bool, noise: NoiseReductionContext,
    orig: WebhookEvent | None, source: str
) -> ForwardDecision:
    """转发决策逻辑"""
    from core.config import Config
    if noise and noise.suppress_forward:
        logger.debug("[Forward] 决策=抑制 reason=noise_%s", noise.relation)
        return ForwardDecision(False, f"智能降噪抑制转发: {noise.reason}", False)

    # 1. 匹配规则
    matched_rules = []
    total_rules = 0
    try:
        async with session_scope() as sess:
            rules_stmt = select(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc())
            rules = (await sess.execute(rules_stmt)).scalars().all()
            total_rules = len(rules)
            for r in rules:
                if r.match_importance and importance not in [
                    x.strip().lower() for x in r.match_importance.split(",")
                ]:
                    continue
                if r.match_source and source.lower() not in [
                    x.strip().lower() for x in r.match_source.split(",")
                ]:
                    continue
                matched_rules.append(r.to_dict())
                if r.stop_on_match:
                    break
        logger.debug("[Forward] 规则匹配完成 total_rules=%d matched=%d importance=%s source=%s",
                     total_rules, len(matched_rules), importance, source)
    except Exception as e:
        logger.warning("[Pipeline] 匹配转发规则失败: %s", e)

    # 2. 组合决策
    should_fwd, is_periodic, skip_reason = False, False, None
    suppressed = False  # 去重/冷却期明确禁止推送
    if is_duplicate:
        if orig and orig.last_notified_at and (
            datetime.now() - orig.last_notified_at
        ).total_seconds() < Config.retry.NOTIFICATION_COOLDOWN_SECONDS:
            suppressed, skip_reason = True, "窗口内重复告警，刚刚已转发"
        elif Config.retry.ENABLE_PERIODIC_REMINDER and orig and orig.last_notified_at and (
            datetime.now() - orig.last_notified_at
        ).total_seconds() / 3600 >= Config.retry.REMINDER_INTERVAL_HOURS:
            should_fwd, is_periodic = True, True
        else:
            if not Config.retry.FORWARD_DUPLICATE_ALERTS:
                suppressed, skip_reason = True, "窗口内重复告警，配置跳过转发"
            else:
                should_fwd = True
    elif beyond_window:
        if not Config.retry.FORWARD_AFTER_TIME_WINDOW:
            suppressed, skip_reason = True, "窗口外重复告警，配置不转发"
        elif orig and orig.last_notified_at and (
            datetime.now() - orig.last_notified_at
        ).total_seconds() < Config.retry.NOTIFICATION_COOLDOWN_SECONDS:
            suppressed, skip_reason = True, "窗口外重复告警，刚刚已转发"
        else:
            should_fwd = True
    else:
        should_fwd = (importance == "high" or bool(matched_rules))
        skip_reason = f"重要性为 {importance}，非高风险事件不自动转发" if not should_fwd else None

    # 去重/冷却期明确禁止时，规则匹配不能覆盖
    final_forward = False if suppressed else (should_fwd or bool(matched_rules))

    if final_forward:
        logger.debug("[Forward] 决策=转发 is_periodic=%s matched_rules=%d skip_reason=%s",
                     is_periodic, len(matched_rules), skip_reason)
    else:
        logger.debug("[Forward] 决策=跳过 reason=%s suppressed=%s",
                     skip_reason or "no_match", suppressed)

    return ForwardDecision(
        should_forward=final_forward,
        skip_reason=skip_reason if not final_forward else None,
        is_periodic_reminder=is_periodic,
        matched_rules=matched_rules if not suppressed else []
    )


async def _execute_forwarding(
    decision: ForwardDecision, full_data: dict, analysis: dict, webhook_id: int, orig_id: int | None
) -> None:
    """执行转发"""
    from core.config import Config
    if not decision.should_forward:
        return

    tasks = []
    if decision.matched_rules:
        for r in decision.matched_rules:
            if r["target_type"] == "openclaw":
                coro = forward_to_openclaw(full_data, analysis)
            elif not r.get("target_url"):
                logger.warning("[Pipeline] 转发规则 '%s' target_url 为空，跳过", r.get("name", r.get("id")))
                continue
            else:
                coro = forward_to_remote(
                    full_data, analysis, target_url=r["target_url"],
                    is_periodic_reminder=decision.is_periodic_reminder
                )
            tasks.append((r, coro))
    else:
        tasks.append((
            {"name": "default", "target_url": Config.ai.FORWARD_URL, "target_type": "webhook"},
            forward_to_remote(full_data, analysis, is_periodic_reminder=decision.is_periodic_reminder)
        ))

    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
    success = False
    for (rule, _), res in zip(tasks, results):
        is_pending = isinstance(res, dict) and res.get("_pending")
        is_success = isinstance(res, dict) and (res.get("status") == "success" or is_pending)
        rule_name = rule.get("name") or rule.get("id", "default")
        target_url = rule.get("target_url", "")
        if isinstance(res, Exception) or not is_success:
            logger.warning(
                "[Forward] 转发失败 rule=%s target=%s event_id=%s error=%s",
                rule_name, target_url, webhook_id, res
            )
            # webhook_id may be a file path string if save_webhook_data fell back to file storage
            wh_id = webhook_id if isinstance(webhook_id, int) else None
            if wh_id is not None:
                await record_failed_forward(
                    wh_id, rule.get("id"), target_url,
                    rule.get("target_type", "webhook"), "error", str(res), full_data
                )
        else:
            success = True
            if is_pending:
                logger.info(
                    "[Forward] 深度分析已提交 rule=%s event_id=%s run_id=%s",
                    rule_name, webhook_id, res.get("_openclaw_run_id", "")
                )
            else:
                logger.info(
                    "[Forward] 转发成功 rule=%s target=%s event_id=%s",
                    rule_name, target_url, webhook_id
                )
            if rule["target_type"] == "openclaw" and is_pending and isinstance(webhook_id, int):
                # 深度分析记录挂在原始事件上，重复事件时用 orig_id
                target_event_id = orig_id if orig_id else webhook_id
                async with session_scope() as sess:
                    sess.add(DeepAnalysis(
                        webhook_event_id=target_event_id, engine="openclaw",
                        openclaw_run_id=res.get("_openclaw_run_id", ""),
                        openclaw_session_key=res.get("_openclaw_session_key", ""), status="pending"
                    ))

    if success and orig_id:
        async with session_scope() as sess:
            await sess.execute(update(WebhookEvent).where(WebhookEvent.id == orig_id).values(
                last_notified_at=datetime.now()
            ))


# ── 主入口 ───────────────────────────────────────────────────────────────────


async def handle_webhook_process(event_id: int, client_ip: str = "", session: Any = None):
    set_trace_id(generate_trace_id(event_id=event_id))
    clear_log_context()
    set_log_context(event_id=event_id)
    task = asyncio.current_task()
    if task:
        _running_tasks.add(task)
        WEBHOOK_RUNNING_TASKS.inc()
    try:
        await _handle_webhook_process_inner(event_id, client_ip, session=session)
    finally:
        if task:
            _running_tasks.discard(task)
            WEBHOOK_RUNNING_TASKS.dec()


async def _handle_webhook_process_inner(event_id: int, client_ip: str = "", session: Any = None):
    start_perf = time.perf_counter()
    outcome, metric_source = "unknown", "unknown"
    try:
        # 始终使用独立 session 更新状态并立即提交，释放行锁
        # 若使用 existing_session（TaskIQ 注入），写操作在整个任务结束前不会 commit，
        # 导致后续 save_webhook_data 的 UPDATE 因等待同一行锁而触发 statement_timeout
        async with session_scope() as sess:
            stmt = update(WebhookEvent).where(WebhookEvent.id == event_id).values(
                processing_status="analyzing", failure_reason=None, error_message=None
            ).returning(WebhookEvent)
            res = await sess.execute(stmt)
            event = res.scalar_one_or_none()
            if not event:
                return
            headers = event.headers or {}
            payload, raw_text = await _load_event_payload(event)
            source = event.source
            raw_body = raw_text.encode("utf-8") if raw_text else b""
            event_ts = event.timestamp.isoformat() if event.timestamp else None

        metric_source = sanitize_source(source or "")
        WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="analyzing").inc()
        WEBHOOK_RECEIVED_TOTAL.labels(source=metric_source, status="received").inc()

        req_ctx = _parse_request(client_ip, headers, payload or {}, raw_body, source, event_ts)
        alert_hash = WebhookEvent.generate_hash(req_ctx.parsed_data, req_ctx.source)
        set_log_context(alert_hash=alert_hash, source=req_ctx.source or "unknown")
        logger.info("[Pipeline] 开始处理 event_id=%s source=%s adapter=%s",
                    event_id, req_ctx.source, req_ctx.parsed_data.get("_adapter", req_ctx.source))

        async with processing_lock(alert_hash) as lock_res:
            if getattr(lock_res, "suppressed", False):
                logger.info("[Pipeline] 告警风暴背压抑制 event_id=%s queue_size=%s",
                            event_id, getattr(lock_res, "queue_size", 0))
                noise = NoiseReductionContext("storm", None, 1.0, True, "alert_storm_backpressure", getattr(lock_res, "queue_size", 0), [])
                await save_webhook_data(
                    data=req_ctx.parsed_data, source=req_ctx.source, raw_payload=req_ctx.payload,
                    headers=req_ctx.headers, client_ip=req_ctx.client_ip,
                    ai_analysis={"noise_reduction": noise.__dict__}, forward_status="skipped",
                    alert_hash=alert_hash, event_id=event_id
                )
                outcome = "suppressed"
                return

            analysis_res = await _resolve_analysis(alert_hash, req_ctx.webhook_full_data, lock_res.got_lock)
            if not lock_res.got_lock:
                logger.debug("[Pipeline] 锁竞争等待完成 event_id=%s got_cached=%s",
                             event_id, bool(analysis_res.analysis_result))

        route_type = analysis_res.analysis_result.get("_route_type", "ai")
        importance = str(analysis_res.analysis_result.get("importance", "unknown")).lower()
        if analysis_res.is_reused:
            set_log_context(route_type=route_type)
            logger.info("[Pipeline] 分析结果复用(redis) event_id=%s importance=%s", event_id, importance)
            noise = NoiseReductionContext("standalone", None, 0.0, False, "缓存复用路径", 0, [])
        else:
            set_log_context(route_type=route_type)
            logger.info("[Pipeline] 分析完成 event_id=%s route=%s importance=%s degraded=%s",
                        event_id, route_type, importance,
                        analysis_res.analysis_result.get("_degraded", False))
            noise = await _compute_noise(alert_hash, req_ctx.source, req_ctx.parsed_data, analysis_res.analysis_result)

        final_analysis = dict(analysis_res.analysis_result)
        final_analysis["noise_reduction"] = noise.__dict__

        save_res = await save_webhook_data(
            data=req_ctx.parsed_data, source=req_ctx.source, raw_payload=req_ctx.payload,
            headers=req_ctx.headers, client_ip=req_ctx.client_ip, ai_analysis=final_analysis,
            alert_hash=alert_hash, is_duplicate=analysis_res.is_duplicate or analysis_res.beyond_window,
            original_event=analysis_res.original_event, beyond_window=analysis_res.beyond_window,
            reanalyzed=analysis_res.reanalyzed, event_id=event_id
        )

        if not analysis_res.is_reused:
            fwd_dec = await _decide_forwarding(
                str(final_analysis.get("importance", "")).lower(),
                save_res.is_duplicate and not save_res.beyond_window,
                save_res.beyond_window, noise, analysis_res.original_event, req_ctx.source
            )
            await _execute_forwarding(fwd_dec, req_ctx.webhook_full_data, final_analysis, save_res.webhook_id, save_res.original_id)

        event_type = "beyond_window" if save_res.beyond_window else ("duplicate" if save_res.is_duplicate else "new")
        importance = str(final_analysis.get("importance", "unknown")).lower()
        route_label = final_analysis.get("_route_type", "ai")
        noise_relation = noise.relation if noise else "unknown"
        duration_ms = int((time.perf_counter() - start_perf) * 1000)
        if not analysis_res.is_reused:
            if fwd_dec.should_forward:
                fwd_info = f" forward=yes rules={len(fwd_dec.matched_rules)}"
                if fwd_dec.is_periodic_reminder:
                    fwd_info += "(periodic)"
            else:
                fwd_info = f" forward=no skip={fwd_dec.skip_reason or 'unknown'}"
        else:
            fwd_info = " forward=skipped(reused)"
        logger.info(
            "[Pipeline] 处理完成 event_id=%s type=%s importance=%s route=%s noise=%s%s duration=%dms",
            event_id, event_type, importance, route_label, noise_relation, fwd_info, duration_ms,
        )

        outcome = "completed"
        set_log_context(processing_status="completed")
        WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="completed").inc()

    except Exception as e:
        retryable = _is_retryable(e)
        status = "received" if retryable else "dead_letter"
        async with session_scope() as sess:
            await sess.execute(update(WebhookEvent).where(WebhookEvent.id == event_id).values(
                processing_status=status, failure_reason="retry_err" if retryable else "fat_err",
                error_message=str(e)[:2000]
            ))
        if status == "dead_letter":
            await _send_dead_letter_alert(event_id, 0, e)
        outcome = "retry" if retryable else "dead_letter"
        logger.error("[Pipeline] 处理失败 event_id=%s retryable=%s error=%s", event_id, retryable, e, exc_info=True)
    finally:
        duration = time.perf_counter() - start_perf
        WEBHOOK_PROCESSING_DURATION_SECONDS.labels(source=metric_source, outcome=outcome).observe(duration)


def get_running_tasks():
    return _running_tasks
