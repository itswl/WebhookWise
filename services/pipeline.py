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
from api import (
    AnalysisResolution,
    NoiseReductionContext,
    WebhookRequestContext,
)
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
    except Exception:
        pass


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
                    await log_ai_usage(
                        route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", "")
                    )
                    return AnalysisResolution(cached, False, True, None, False, is_reused=True)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    # 持锁或等待超时：正常检查/分析
    async with session_scope() as session:
        check = await WebhookEvent.check_duplicate(alert_hash, session=session)
    orig, last_beyond = check.original_event, check.last_beyond_window_event

    if check.beyond_window and orig:
        if last_beyond and last_beyond.created_at and (
            datetime.now() - last_beyond.created_at
        ).total_seconds() < Config.retry.RECENT_BEYOND_WINDOW_REUSE_SECONDS:
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution(last_beyond.ai_analysis or {}, False, True, orig, True)
        if not Config.retry.REANALYZE_AFTER_TIME_WINDOW:
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution(orig.ai_analysis or {}, False, True, orig, True)
        res, rean = await analyze_webhook_with_ai(full_data), True
    elif check.is_duplicate and orig:
        target = last_beyond if last_beyond and last_beyond.ai_analysis else orig
        if target.ai_analysis:
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution(target.ai_analysis, False, True, orig, False)
        res, rean = await analyze_webhook_with_ai(full_data), True
    else:
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
    return NoiseReductionContext(
        dec.relation, dec.root_cause_event_id, dec.confidence,
        dec.suppress_forward, dec.reason, dec.related_alert_count, dec.related_alert_ids
    )


async def _handle_forwarding(
    analysis: dict, is_dup: bool, beyond: bool, noise: NoiseReductionContext,
    orig: WebhookEvent | None, full_data: dict, webhook_id: int
):
    from core.config import Config
    if noise.suppress_forward:
        logger.info(f"智能降噪抑制转发: {noise.reason}")
        return

    # 1. 决策
    importance = str(analysis.get("importance", "")).lower()
    should_fwd, is_periodic = False, False

    # 匹配规则
    matched_rules = []
    try:
        async with session_scope() as sess:
            rules_stmt = select(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc())
            rules = (await sess.execute(rules_stmt)).scalars().all()
            for r in rules:
                if r.match_importance and importance not in [
                    x.strip().lower() for x in r.match_importance.split(",")
                ]:
                    continue
                if r.match_source and full_data.get("source", "").lower() not in [
                    x.strip().lower() for x in r.match_source.split(",")
                ]:
                    continue
                matched_rules.append(r.to_dict())
                if r.stop_on_match:
                    break
    except Exception:
        pass

    if is_dup:
        if orig and orig.last_notified_at and (
            datetime.now() - orig.last_notified_at
        ).total_seconds() < Config.retry.NOTIFICATION_COOLDOWN_SECONDS:
            should_fwd = False
        elif Config.retry.ENABLE_PERIODIC_REMINDER and orig and orig.last_notified_at and (
            datetime.now() - orig.last_notified_at
        ).total_seconds() / 3600 >= Config.retry.REMINDER_INTERVAL_HOURS:
            should_fwd, is_periodic = True, True
        else:
            should_fwd = Config.retry.FORWARD_DUPLICATE_ALERTS
    elif beyond:
        should_fwd = Config.retry.FORWARD_AFTER_TIME_WINDOW and not (
            orig and orig.last_notified_at and (
                datetime.now() - orig.last_notified_at
            ).total_seconds() < Config.retry.NOTIFICATION_COOLDOWN_SECONDS
        )
    else:
        should_fwd = (importance == "high" or bool(matched_rules))

    if not should_fwd and not matched_rules:
        return

    # 2. 执行
    tasks = []
    if matched_rules:
        for r in matched_rules:
            if r["target_type"] == "openclaw":
                coro = forward_to_openclaw(full_data, analysis)
            else:
                coro = forward_to_remote(
                    full_data, analysis, target_url=r["target_url"], is_periodic_reminder=is_periodic
                )
            tasks.append((r, coro))
    else:
        tasks.append((
            {"name": "default", "target_url": Config.ai.FORWARD_URL, "target_type": "webhook"},
            forward_to_remote(full_data, analysis, is_periodic_reminder=is_periodic)
        ))

    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
    success = False
    for (rule, _), res in zip(tasks, results):
        if isinstance(res, Exception) or (isinstance(res, dict) and res.get("status") != "success"):
            await record_failed_forward(
                webhook_id, rule.get("id"), rule.get("target_url", ""),
                rule.get("target_type", "webhook"), "error", str(res), full_data
            )
        else:
            success = True
            if rule["target_type"] == "openclaw" and res.get("_pending"):
                async with session_scope() as sess:
                    sess.add(DeepAnalysis(
                        webhook_event_id=webhook_id, engine="openclaw",
                        openclaw_run_id=res.get("run_id", ""),
                        openclaw_session_key=res.get("session_key", ""), status="pending"
                    ))

    if success and orig:
        async with session_scope() as sess:
            await sess.execute(update(WebhookEvent).where(WebhookEvent.id == orig.id).values(
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
        async with session_scope(existing_session=session) as sess:
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

        async with processing_lock(alert_hash) as lock_res:
            if getattr(lock_res, "suppressed", False):
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

        if analysis_res.is_reused:
            set_log_context(route_type="cache")
            noise = NoiseReductionContext("standalone", None, 0.0, False, "缓存复用路径", 0, [])
        else:
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
            await _handle_forwarding(
                final_analysis, save_res.is_duplicate and not save_res.beyond_window,
                save_res.beyond_window, noise, analysis_res.original_event,
                req_ctx.webhook_full_data, save_res.webhook_id
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
        logger.error(f"处理失败 (retryable={retryable}): {e}", exc_info=True)
    finally:
        duration = time.perf_counter() - start_perf
        WEBHOOK_PROCESSING_DURATION_SECONDS.labels(source=metric_source, outcome=outcome).observe(duration)


def get_running_tasks():
    return _running_tasks
