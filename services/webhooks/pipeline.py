"""Webhook 处理主管线 — 纯协调层。
集成解析、分析决策、降噪计算与转发执行。
"""

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.ecosystem_adapters import normalize_webhook_event
from core.config import Config
from core.distributed_lock import processing_lock
from core.log_context import clear_log_context, set_log_context
from core.logger import logger
from core.metrics import (
    WEBHOOK_DEAD_LETTER_TOTAL,
    WEBHOOK_NOISE_REDUCED_TOTAL,
    WEBHOOK_PROCESSING_DURATION_SECONDS,
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    WEBHOOK_RUNNING_TASKS,
    WEBHOOK_STORM_SUPPRESSED_TOTAL,
    sanitize_source,
)
from core.otel import span as otel_span
from core.retry_policies import retry_policy
from core.trace import generate_trace_id, set_trace_id
from db.session import session_scope
from models import WebhookEvent
from services.analysis.ai_analyzer import analyze_webhook_with_ai, get_cached_analysis, log_ai_usage
from services.analysis.noise_reduction import AlertContext, NoiseScoringConfig, analyze_noise_reduction
from services.forwarding.forward import forward_to_openclaw, forward_to_remote, record_failed_forward
from services.operations.taskiq_retry_scheduler import compute_backoff_delay, schedule_webhook_retry
from services.webhooks.command_service import save_webhook_data
from services.webhooks.decisioning import (
    ForwardingPolicy,
    ForwardRuleSnapshot,
    build_final_analysis,
    decide_forwarding,
    normalize_importance,
)
from services.webhooks.repository import (
    create_openclaw_analysis,
    list_enabled_forward_rules,
    list_recent_alert_contexts,
    mark_dead_letter,
    mark_last_notified,
    mark_retry,
    mark_retry_enqueue_failed,
    transition_to_analyzing_and_load,
)
from services.webhooks.types import (
    AnalysisResolution,
    ForwardDecision,
    NoiseReductionContext,
    WebhookProcessingStatus,
    WebhookRequestContext,
)


def _normalize_importance(value: Any) -> str:
    return normalize_importance(value)


_running_tasks: set[asyncio.Task[object]] = set()


# ── 核心辅助 ──────────────────────────────────────────────────────────────────


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
                            "content": f"**event_id**: {event_id}\n**重试**: {retry_count}\n**详情**: {str(error)[:200]}",
                        },
                    }
                ],
            },
        }
        await get_http_client().post(url, json=card, timeout=10)
    except Exception as e:
        logger.warning("[Pipeline] 发送死信告警失败: %s", e)


# ── 阶段逻辑 ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _PipelineContext:
    event_id: int
    client_ip: str
    metric_source: str
    req_ctx: WebhookRequestContext
    alert_hash: str


def _parse_request(
    client_ip: str,
    headers: dict[str, Any],
    payload: dict[str, Any],
    raw_body: bytes,
    source: str | None,
    ts: str | None,
) -> WebhookRequestContext:
    src = source or headers.get("x-webhook-source", "unknown")
    if not payload and raw_body:
        loaded = orjson.loads(raw_body)
        payload = loaded if isinstance(loaded, dict) else {}
    norm = normalize_webhook_event(payload, src)
    return WebhookRequestContext(
        client_ip=client_ip,
        source=norm.source,
        payload=raw_body,
        parsed_data=norm.data,
        webhook_full_data={
            "body": payload,
            "headers": headers,
            "parsed_data": norm.data,
            "source": norm.source,
            "timestamp": ts,
        },
        headers=headers,
    )


async def _resolve_analysis(alert_hash: str, full_data: dict[str, Any], got_lock: bool) -> AnalysisResolution:
    from core.config import Config

    if not got_lock:
        # 锁竞争逻辑：Pub/Sub 等待
        from core.redis_client import redis_pubsub

        channel = f"analysis_done:{alert_hash}"
        pubsub = redis_pubsub()
        try:
            await pubsub.subscribe(channel)
            cached = await get_cached_analysis(alert_hash)
            if cached:
                logger.debug("[Pipeline] 锁竞争立即命中缓存 hash=%s...", alert_hash[:12])
                await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
                return AnalysisResolution(cached, False, False, None, False, is_reused=True)
            deadline = time.monotonic() + Config.retry.PROCESSING_LOCK_WAIT_SECONDS
            while time.monotonic() < deadline:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0),
                        timeout=6.0,
                    )
                cached = await get_cached_analysis(alert_hash)
                if cached:
                    logger.debug("[Pipeline] 锁竞争 pub/sub 等待后命中缓存 hash=%s...", alert_hash[:12])
                    await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
                    return AnalysisResolution(cached, False, False, None, False, is_reused=True)
            logger.debug("[Pipeline] 锁竞争等待超时，降级为独立分析 hash=%s...", alert_hash[:12])
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    # 持锁或等待超时：正常检查/分析
    async with session_scope() as session:
        check = await WebhookEvent.check_duplicate(
            alert_hash, session=session, time_window_hours=Config.retry.DUPLICATE_ALERT_TIME_WINDOW
        )
    orig, last_beyond = check.original_event, check.last_beyond_window_event

    if check.beyond_window and orig:
        if (
            last_beyond
            and last_beyond.created_at
            and (datetime.now() - last_beyond.created_at).total_seconds()
            < Config.retry.RECENT_BEYOND_WINDOW_REUSE_SECONDS
            and not (last_beyond.ai_analysis or {}).get("_degraded")
        ):
            logger.debug(
                "[Pipeline] 窗口外复用最近 beyond_window 事件分析 orig_id=%s hash=%s...", orig.id, alert_hash[:12]
            )
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution(
                {**(last_beyond.ai_analysis or {}), "_route_type": "db_reuse"}, False, True, orig, True
            )
        if not Config.retry.REANALYZE_AFTER_TIME_WINDOW and not (orig.ai_analysis or {}).get("_degraded"):
            logger.debug("[Pipeline] 窗口外复用原始事件分析 orig_id=%s hash=%s...", orig.id, alert_hash[:12])
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution({**(orig.ai_analysis or {}), "_route_type": "db_reuse"}, False, True, orig, True)
        logger.debug(
            "[Pipeline] 窗口外重新分析 orig_id=%s reason=%s hash=%s...",
            orig.id,
            "reanalyze_enabled" if Config.retry.REANALYZE_AFTER_TIME_WINDOW else "prev_degraded",
            alert_hash[:12],
        )
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


async def _compute_noise(
    alert_hash: str, source: str, parsed: dict[str, Any], analysis: dict[str, Any]
) -> NoiseReductionContext:
    from core.config import Config

    if not Config.ai.ENABLE_ALERT_NOISE_REDUCTION:
        return NoiseReductionContext("standalone", None, 0.0, False, "智能降噪未启用", 0, [])
    now = datetime.now()
    window = max(1, Config.ai.NOISE_REDUCTION_WINDOW_MINUTES)
    try:
        recent = await list_recent_alert_contexts(alert_hash, now, window)
    except Exception:
        recent = []
    curr = AlertContext(
        None, source, _normalize_importance(analysis.get("importance", "medium")), parsed, analysis, now, alert_hash
    )
    dec = analyze_noise_reduction(
        curr,
        recent,
        window_minutes=window,
        min_confidence=Config.ai.ROOT_CAUSE_MIN_CONFIDENCE,
        suppress_derived=Config.ai.SUPPRESS_DERIVED_ALERT_FORWARD,
        scoring_config=NoiseScoringConfig.from_runtime_config(Config.ai),
    )
    if dec.suppress_forward:
        logger.info(
            "[Noise] 抑制转发 relation=%s root_cause_id=%s confidence=%.2f reason=%s",
            dec.relation,
            dec.root_cause_event_id,
            dec.confidence,
            dec.reason,
        )
    elif dec.relation != "standalone":
        logger.debug(
            "[Noise] 关联但不抑制 relation=%s root_cause_id=%s confidence=%.2f",
            dec.relation,
            dec.root_cause_event_id,
            dec.confidence,
        )
    return NoiseReductionContext(
        dec.relation,
        dec.root_cause_event_id,
        dec.confidence,
        dec.suppress_forward,
        dec.reason,
        dec.related_alert_count,
        dec.related_alert_ids,
    )


async def _decide_forwarding(
    importance: str,
    is_duplicate: bool,
    beyond_window: bool,
    noise: NoiseReductionContext | None,
    orig: WebhookEvent | None,
    source: str,
) -> ForwardDecision:
    """转发决策逻辑"""
    from core.config import Config

    rules: list[ForwardRuleSnapshot] = []
    try:
        rules = await list_enabled_forward_rules()
    except Exception as e:
        logger.warning("[Pipeline] 匹配转发规则失败: %s", e)

    decision = decide_forwarding(
        importance=importance,
        is_duplicate=is_duplicate,
        beyond_window=beyond_window,
        noise=noise,
        original_event=orig,
        source=source,
        rules=rules,
        policy=ForwardingPolicy(
            notification_cooldown_seconds=Config.retry.NOTIFICATION_COOLDOWN_SECONDS,
            enable_periodic_reminder=Config.retry.ENABLE_PERIODIC_REMINDER,
            reminder_interval_hours=Config.retry.REMINDER_INTERVAL_HOURS,
            forward_duplicate_alerts=Config.retry.FORWARD_DUPLICATE_ALERTS,
            forward_after_time_window=Config.retry.FORWARD_AFTER_TIME_WINDOW,
        ),
    )

    if decision.should_forward:
        logger.debug(
            "[Forward] 决策=转发 is_periodic=%s matched_rules=%d skip_reason=%s",
            decision.is_periodic_reminder,
            len(decision.matched_rules),
            decision.skip_reason,
        )
    else:
        logger.debug("[Forward] 决策=跳过 reason=%s", decision.skip_reason or "no_match")

    return decision


async def _execute_forwarding(
    decision: ForwardDecision,
    full_data: dict[str, Any],
    analysis: dict[str, Any],
    webhook_id: int,
    orig_id: int | None,
) -> None:
    """执行转发"""
    from core.config import Config

    if not decision.should_forward:
        return

    tasks: list[tuple[dict[str, Any], Any]] = []
    if decision.matched_rules:
        for r in decision.matched_rules:
            if r.get("target_type") == "openclaw":
                coro = forward_to_openclaw(full_data, analysis)
            elif not r.get("target_url"):
                logger.warning("[Pipeline] 转发规则 '%s' target_url 为空，跳过", r.get("name", r.get("id")))
                continue
            else:
                coro = forward_to_remote(
                    full_data,
                    analysis,
                    target_url=str(r.get("target_url") or ""),
                    is_periodic_reminder=decision.is_periodic_reminder,
                )
            tasks.append((r, coro))
    else:
        tasks.append(
            (
                {"name": "default", "target_url": Config.ai.FORWARD_URL, "target_type": "webhook"},
                forward_to_remote(full_data, analysis, is_periodic_reminder=decision.is_periodic_reminder),
            )
        )

    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
    success = False
    for (rule, _), res in zip(tasks, results, strict=False):
        res_dict = res if isinstance(res, dict) else None
        is_pending = bool(res_dict and res_dict.get("_pending"))
        is_success = bool(res_dict and (res_dict.get("status") == "success" or is_pending))
        rule_name = rule.get("name") or rule.get("id", "default")
        target_url = str(rule.get("target_url", "") or "")
        if isinstance(res, Exception) or not is_success:
            logger.warning(
                "[Forward] 转发失败 rule=%s target=%s event_id=%s error=%s", rule_name, target_url, webhook_id, res
            )
            raw_rule_id = rule.get("id")
            rule_id: int | None = None
            if isinstance(raw_rule_id, int):
                rule_id = raw_rule_id
            elif isinstance(raw_rule_id, str):
                with contextlib.suppress(ValueError):
                    rule_id = int(raw_rule_id)
            await record_failed_forward(
                webhook_id,
                rule_id,
                target_url,
                str(rule.get("target_type", "webhook") or "webhook"),
                "error",
                str(res),
                full_data,
            )
        else:
            success = True
            if is_pending:
                logger.info(
                    "[Forward] 深度分析已提交 rule=%s event_id=%s run_id=%s",
                    rule_name,
                    webhook_id,
                    (res_dict or {}).get("_openclaw_run_id", ""),
                )
            else:
                logger.info("[Forward] 转发成功 rule=%s target=%s event_id=%s", rule_name, target_url, webhook_id)
            if rule.get("target_type") == "openclaw" and is_pending:
                # 深度分析记录挂在原始事件上，重复事件时用 orig_id
                target_event_id = orig_id if orig_id else webhook_id
                analysis_id = await create_openclaw_analysis(
                    target_event_id,
                    run_id=str(cast(dict[str, Any], res).get("_openclaw_run_id", "")),
                    session_key=str(cast(dict[str, Any], res).get("_openclaw_session_key", "")),
                )
                try:
                    from services.operations.taskiq_retry_scheduler import schedule_openclaw_poll

                    await schedule_openclaw_poll(analysis_id, Config.openclaw.OPENCLAW_MIN_WAIT_SECONDS)
                except Exception as e:
                    logger.warning("[Forward] OpenClaw poll 调度失败 analysis_id=%s error=%s", analysis_id, e)

    if success and orig_id:
        await mark_last_notified(orig_id)


# ── 主入口 ───────────────────────────────────────────────────────────────────


async def _handle_storm_suppression(ctx: _PipelineContext, lock_res: object) -> bool:
    if not getattr(lock_res, "suppressed", False):
        return False
    logger.info(
        "[Pipeline] 告警风暴背压抑制 event_id=%s queue_size=%s",
        ctx.event_id,
        getattr(lock_res, "queue_size", 0),
    )
    WEBHOOK_STORM_SUPPRESSED_TOTAL.labels(source=ctx.metric_source).inc()
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="suppressed").inc()
    noise = NoiseReductionContext(
        "storm",
        None,
        1.0,
        True,
        "alert_storm_backpressure",
        getattr(lock_res, "queue_size", 0),
        [],
    )
    await save_webhook_data(
        data=ctx.req_ctx.parsed_data,
        source=ctx.req_ctx.source,
        raw_payload=ctx.req_ctx.payload,
        headers=ctx.req_ctx.headers,
        client_ip=ctx.req_ctx.client_ip,
        ai_analysis={"noise_reduction": noise.__dict__},
        forward_status="skipped",
        alert_hash=ctx.alert_hash,
        event_id=ctx.event_id,
    )
    return True


def _build_final_analysis(analysis_result: dict[str, Any], noise: NoiseReductionContext) -> dict[str, Any]:
    return build_final_analysis(analysis_result, noise)


async def _persist_analysis_result(
    ctx: _PipelineContext,
    analysis_res: AnalysisResolution,
    final_analysis: dict[str, Any],
) -> Any:
    is_dup_for_save: bool | None = analysis_res.is_duplicate or analysis_res.beyond_window
    original_for_save = analysis_res.original_event
    beyond_for_save = analysis_res.beyond_window
    if original_for_save is None:
        is_dup_for_save = None
        beyond_for_save = False

    return await save_webhook_data(
        data=ctx.req_ctx.parsed_data,
        source=ctx.req_ctx.source,
        raw_payload=ctx.req_ctx.payload,
        headers=ctx.req_ctx.headers,
        client_ip=ctx.req_ctx.client_ip,
        ai_analysis=final_analysis,
        alert_hash=ctx.alert_hash,
        is_duplicate=is_dup_for_save,
        original_event=original_for_save,
        beyond_window=beyond_for_save,
        reanalyzed=analysis_res.reanalyzed,
        event_id=ctx.event_id,
    )


async def _maybe_forward(
    ctx: _PipelineContext,
    analysis_res: AnalysisResolution,
    save_res: Any,
    final_analysis: dict[str, Any],
    noise: NoiseReductionContext,
) -> ForwardDecision | None:
    if analysis_res.is_reused:
        return None
    fwd_dec = await _decide_forwarding(
        _normalize_importance(final_analysis.get("importance", "")),
        bool(save_res.is_duplicate) and not bool(save_res.beyond_window),
        bool(save_res.beyond_window),
        noise,
        analysis_res.original_event,
        ctx.req_ctx.source,
    )
    await _execute_forwarding(
        fwd_dec, ctx.req_ctx.webhook_full_data, final_analysis, save_res.webhook_id, save_res.original_id
    )
    return fwd_dec


async def _handle_process_exception(event_id: int, err: Exception, span: Any | None) -> str:
    retryable = retry_policy.should_retry(err)
    max_retries = max(0, Config.retry.WEBHOOK_RETRY_MAX_RETRIES)
    next_retry_count = 0
    status = "dead_letter"
    if retryable:
        marked_retry_count = await mark_retry(event_id, max_retries=max_retries, error_message=str(err))
        if marked_retry_count is not None:
            next_retry_count = marked_retry_count
            status = "retry"

        if status == "retry":
            delay = compute_backoff_delay(
                next_retry_count,
                initial_delay=Config.retry.WEBHOOK_RETRY_INITIAL_DELAY,
                max_delay=Config.retry.WEBHOOK_RETRY_MAX_DELAY,
                multiplier=Config.retry.WEBHOOK_RETRY_BACKOFF_MULTIPLIER,
            )
            try:
                await schedule_webhook_retry(event_id, delay)
            except Exception as enqueue_err:
                await mark_retry_enqueue_failed(event_id, str(enqueue_err))
                logger.warning(
                    "[Pipeline] TaskIQ 延迟重试调度失败，回落到 recovery 兜底 event_id=%s error=%s",
                    event_id,
                    enqueue_err,
                )
                outcome = "retry_enqueue_failed"
                WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status=outcome).inc()
                return outcome

            outcome = "retry"
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status=outcome).inc()
            logger.error(
                "[Pipeline] 处理失败，已进入 TaskIQ 延迟重试 event_id=%s retry=%s/%s delay=%ss error=%s",
                event_id,
                next_retry_count,
                max_retries,
                delay,
                err,
                exc_info=True,
            )
            if span:
                with contextlib.suppress(Exception):
                    from opentelemetry.trace import StatusCode

                    span.set_status(StatusCode.ERROR, str(err))
            return outcome

    await mark_dead_letter(event_id, retryable=retryable, error_message=str(err))
    await _send_dead_letter_alert(event_id, next_retry_count, err)
    WEBHOOK_DEAD_LETTER_TOTAL.inc()
    outcome = "dead_letter"
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status=outcome).inc()
    logger.error("[Pipeline] 处理失败 event_id=%s retryable=%s error=%s", event_id, retryable, err, exc_info=True)
    if span:
        with contextlib.suppress(Exception):
            from opentelemetry.trace import StatusCode

            span.set_status(StatusCode.ERROR, str(err))
    return outcome


async def handle_webhook_process(event_id: int, client_ip: str = "", session: AsyncSession | None = None) -> None:
    set_trace_id(generate_trace_id(event_id=event_id))
    # 若 OTEL 已启用，优先用当前活动 span 的 trace_id 保证日志与 APM 一致
    from core.otel import get_otel_trace_id

    otel_tid = get_otel_trace_id()
    if otel_tid:
        set_trace_id(otel_tid)
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


async def _handle_webhook_process_inner(
    event_id: int, client_ip: str = "", session: AsyncSession | None = None
) -> None:
    start_perf = time.perf_counter()
    outcome, metric_source = "unknown", "unknown"
    with otel_span("webhook.process", {"event_id": event_id}) as _span:
        try:
            env = await transition_to_analyzing_and_load(event_id)
            if not env:
                logger.debug("[Pipeline] 忽略已处理或不存在的事件: event_id=%s", event_id)
                return

            metric_source = sanitize_source(env.source or "")
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="analyzing").inc()
            WEBHOOK_RECEIVED_TOTAL.labels(source=metric_source, status="received").inc()

            req_ctx = _parse_request(client_ip, env.headers, env.payload or {}, env.raw_body, env.source, env.event_ts)
            alert_hash = WebhookEvent.generate_hash(req_ctx.parsed_data, req_ctx.source)
            set_log_context(alert_hash=alert_hash, source=req_ctx.source or "unknown")
            ctx = _PipelineContext(
                event_id=event_id,
                client_ip=client_ip,
                metric_source=metric_source,
                req_ctx=req_ctx,
                alert_hash=alert_hash,
            )
            logger.info(
                "[Pipeline] 开始处理 event_id=%s source=%s adapter=%s",
                event_id,
                req_ctx.source,
                req_ctx.parsed_data.get("_adapter", req_ctx.source),
            )
            if _span:
                _span.set_attribute("source", req_ctx.source or "unknown")
                _span.set_attribute("alert_hash", alert_hash[:12])

            async with processing_lock(alert_hash) as lock_res:
                if await _handle_storm_suppression(ctx, lock_res):
                    outcome = "suppressed"
                    return

                analysis_res = await _resolve_analysis(alert_hash, req_ctx.webhook_full_data, lock_res.got_lock)
                if not lock_res.got_lock:
                    logger.debug(
                        "[Pipeline] 锁竞争等待完成 event_id=%s got_cached=%s",
                        event_id,
                        bool(analysis_res.analysis_result),
                    )

            route_type = analysis_res.analysis_result.get("_route_type", "ai")
            importance = _normalize_importance(analysis_res.analysis_result.get("importance", "unknown"))
            if analysis_res.is_reused:
                set_log_context(route_type=route_type)
                logger.info("[Pipeline] 分析结果复用(redis) event_id=%s importance=%s", event_id, importance)
                noise = NoiseReductionContext("standalone", None, 0.0, False, "缓存复用路径", 0, [])
            else:
                set_log_context(route_type=route_type)
                logger.info(
                    "[Pipeline] 分析完成 event_id=%s route=%s importance=%s degraded=%s",
                    event_id,
                    route_type,
                    importance,
                    analysis_res.analysis_result.get("_degraded", False),
                )
                noise = await _compute_noise(
                    alert_hash, req_ctx.source, req_ctx.parsed_data, analysis_res.analysis_result
                )

            final_analysis = _build_final_analysis(analysis_res.analysis_result, noise)
            save_res = await _persist_analysis_result(ctx, analysis_res, final_analysis)
            fwd_dec = await _maybe_forward(ctx, analysis_res, save_res, final_analysis, noise)

            event_type = (
                "beyond_window" if save_res.beyond_window else ("duplicate" if save_res.is_duplicate else "new")
            )
            importance = _normalize_importance(final_analysis.get("importance", "unknown"))
            route_label = final_analysis.get("_route_type", "ai")
            noise_relation = noise.relation if noise else "unknown"
            duration_ms = int((time.perf_counter() - start_perf) * 1000)
            if analysis_res.is_reused or fwd_dec is None:
                fwd_info = " forward=skipped(reused)"
            elif fwd_dec.should_forward:
                fwd_info = f" forward=yes rules={len(fwd_dec.matched_rules)}"
                if fwd_dec.is_periodic_reminder:
                    fwd_info += "(periodic)"
            else:
                fwd_info = f" forward=no skip={fwd_dec.skip_reason or 'unknown'}"
            logger.info(
                "[Pipeline] 处理完成 event_id=%s type=%s importance=%s route=%s noise=%s%s duration=%dms",
                event_id,
                event_type,
                importance,
                route_label,
                noise_relation,
                fwd_info,
                duration_ms,
            )

            if _span:
                _span.set_attribute("importance", importance)
                _span.set_attribute("route", route_label)
                _span.set_attribute("event_type", event_type)
                _span.set_attribute("duration_ms", duration_ms)

            WEBHOOK_NOISE_REDUCED_TOTAL.labels(
                source=metric_source,
                relation=noise_relation,
                suppressed=str(noise.suppress_forward).lower() if noise else "false",
            ).inc()

            outcome = "completed"
            set_log_context(processing_status=WebhookProcessingStatus.COMPLETED.value)
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="completed").inc()

        except Exception as e:
            outcome = await _handle_process_exception(event_id, e, _span)
            return
        finally:
            duration = time.perf_counter() - start_perf
            WEBHOOK_PROCESSING_DURATION_SECONDS.labels(source=metric_source, outcome=outcome).observe(duration)


def get_running_tasks() -> set[asyncio.Task[object]]:
    return _running_tasks
