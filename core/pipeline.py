import asyncio
from datetime import datetime, timedelta
from typing import Optional, Union
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from core.config import Config
from core.logger import logger
from core.metrics import WEBHOOK_RECEIVED_TOTAL, WEBHOOK_NOISE_REDUCED_TOTAL
from core.routes import (
    AnalysisResolution,
    ForwardDecision,
    InvalidJsonError,
    InvalidSignatureError,
    NoiseReductionContext,
    PersistedEventContext,
    WebhookRequestContext,
    _ok,
)
from core.utils import (
    check_duplicate_alert,
    generate_alert_hash,
    processing_lock,
    save_webhook_data,
)
from core.webhook_security import ensure_webhook_auth
from adapters.ecosystem_adapters import normalize_webhook_event
from core.models import DeepAnalysis, WebhookEvent, get_session, session_scope
from services.ai_analyzer import analyze_webhook_with_ai, forward_to_remote, log_ai_usage
from services.alert_noise_reduction import AlertContext, analyze_noise_reduction

_LOCK_WAIT_SECONDS = Config.PROCESSING_LOCK_WAIT_SECONDS


def _default_noise_context() -> NoiseReductionContext:
    return NoiseReductionContext(
        relation='standalone',
        root_cause_event_id=None,
        confidence=0.0,
        suppress_forward=False,
        reason='智能降噪未启用',
        related_alert_count=0,
        related_alert_ids=[]
    )


def _build_alert_context(
    event_id: Optional[int],
    source: str,
    parsed_data: dict,
    analysis: dict,
    timestamp: datetime,
    alert_hash: Optional[str] = None,
    importance: Optional[str] = None,
) -> AlertContext:
    derived_importance = str(importance or analysis.get('importance') or '').lower().strip()
    if derived_importance not in {'high', 'medium', 'low'}:
        derived_importance = 'medium'

    return AlertContext(
        event_id=event_id,
        source=source,
        importance=derived_importance,
        parsed_data=parsed_data if isinstance(parsed_data, dict) else {},
        analysis=analysis if isinstance(analysis, dict) else {},
        timestamp=timestamp,
        alert_hash=alert_hash,
    )


def _load_recent_alert_contexts(current_hash: str, current_time: datetime) -> list[AlertContext]:
    window_minutes = max(1, Config.NOISE_REDUCTION_WINDOW_MINUTES)
    time_threshold = current_time - timedelta(minutes=window_minutes)

    try:
        with get_session() as session:
            query = (
                session.query(WebhookEvent)
                .filter(
                    WebhookEvent.timestamp >= time_threshold,
                    WebhookEvent.timestamp <= current_time,
                )
                .order_by(WebhookEvent.timestamp.desc())
                .limit(100)
            )
            events = query.all()
    except Exception as e:
        logger.warning(f"加载降噪候选告警失败: {e}")
        return []

    contexts: list[AlertContext] = []
    for event in events:
        if event.alert_hash == current_hash:
            continue
        contexts.append(
            _build_alert_context(
                event_id=event.id,
                source=event.source,
                parsed_data=event.parsed_data or {},
                analysis=event.ai_analysis or {},
                timestamp=event.timestamp or datetime.now(),
                alert_hash=event.alert_hash,
                importance=event.importance,
            )
        )

    return contexts


def _compute_noise_reduction(
    *,
    alert_hash: str,
    source: str,
    parsed_data: dict,
    analysis_result: dict,
) -> NoiseReductionContext:
    if not Config.ENABLE_ALERT_NOISE_REDUCTION:
        return _default_noise_context()

    now = datetime.now()
    current_ctx = _build_alert_context(
        event_id=None,
        source=source,
        parsed_data=parsed_data,
        analysis=analysis_result,
        timestamp=now,
        alert_hash=alert_hash,
    )

    recent_contexts = _load_recent_alert_contexts(alert_hash, now)
    decision = analyze_noise_reduction(
        current_ctx,
        recent_contexts,
        window_minutes=max(1, Config.NOISE_REDUCTION_WINDOW_MINUTES),
        min_confidence=max(0.0, min(1.0, Config.ROOT_CAUSE_MIN_CONFIDENCE)),
        suppress_derived=Config.SUPPRESS_DERIVED_ALERT_FORWARD,
    )

    return NoiseReductionContext(
        relation=decision.relation,
        root_cause_event_id=decision.root_cause_event_id,
        confidence=decision.confidence,
        suppress_forward=decision.suppress_forward,
        reason=decision.reason,
        related_alert_count=decision.related_alert_count,
        related_alert_ids=decision.related_alert_ids,
    )


def _apply_noise_metadata(analysis_result: dict, noise_context: NoiseReductionContext) -> dict:
    merged = dict(analysis_result)
    merged['noise_reduction'] = {
        'relation': noise_context.relation,
        'root_cause_event_id': noise_context.root_cause_event_id,
        'confidence': noise_context.confidence,
        'suppress_forward': noise_context.suppress_forward,
        'reason': noise_context.reason,
        'related_alert_count': noise_context.related_alert_count,
        'related_alert_ids': noise_context.related_alert_ids,
    }
    return merged


def _persist_webhook_with_noise_context(
    *,
    request_context: WebhookRequestContext,
    analysis_resolution: AnalysisResolution,
    alert_hash: str,
) -> PersistedEventContext:
    noise_context = _compute_noise_reduction(
        alert_hash=alert_hash,
        source=request_context.source,
        parsed_data=request_context.parsed_data,
        analysis_result=analysis_resolution.analysis_result,
    )

    analysis_with_noise = _apply_noise_metadata(analysis_resolution.analysis_result, noise_context)
    save_result = save_webhook_data(
        data=request_context.parsed_data,
        source=request_context.source,
        raw_payload=request_context.payload,
        headers=request_context.headers,
        client_ip=request_context.client_ip,
        ai_analysis=analysis_with_noise,
        alert_hash=alert_hash,
        is_duplicate=analysis_resolution.is_duplicate or analysis_resolution.beyond_window,
        original_event=analysis_resolution.original_event,
        beyond_window=analysis_resolution.beyond_window,
        reanalyzed=analysis_resolution.reanalyzed
    )

    return PersistedEventContext(save_result=save_result, noise_context=noise_context)


async def _analyze_now(webhook_full_data: dict, message: str) -> tuple[dict, bool]:
    logger.info(message)
    return await analyze_webhook_with_ai(webhook_full_data), True


async def _resolve_duplicate_analysis(
    original_event: WebhookEvent,
    last_beyond_window_event: Optional[WebhookEvent],
    webhook_full_data: dict
) -> tuple[dict, bool]:
    if last_beyond_window_event and last_beyond_window_event.ai_analysis:
        logger.info(f"检测到窗口内重复，复用本窗口内最新分析结果 (ID={last_beyond_window_event.id})")
        log_ai_usage(
            route_type='reuse',
            alert_hash=last_beyond_window_event.alert_hash or '',
            source=last_beyond_window_event.source or ''
        )
        return last_beyond_window_event.ai_analysis, False

    if original_event.ai_analysis:
        logger.info(f"复用原始告警 ID={original_event.id} 的分析结果")
        log_ai_usage(
            route_type='reuse',
            alert_hash=original_event.alert_hash or '',
            source=original_event.source or ''
        )
        return original_event.ai_analysis, False

    return await _analyze_now(webhook_full_data, f"原始告警 ID={original_event.id} 缺少AI分析，重新分析")


async def _resolve_beyond_window_analysis(
    original_event: Optional[WebhookEvent],
    last_beyond_window_event: Optional[WebhookEvent],
    webhook_full_data: dict,
    allow_reanalyze: bool,
    prefer_recent_beyond_window: bool
) -> tuple[dict, bool]:
    if prefer_recent_beyond_window and last_beyond_window_event:
        is_recent = False
        if last_beyond_window_event.created_at:
            seconds_since = (datetime.now() - last_beyond_window_event.created_at).total_seconds()
            if seconds_since < Config.RECENT_BEYOND_WINDOW_REUSE_SECONDS:
                is_recent = True

        if is_recent:
            logger.info(f"窗口外历史告警，发现其他worker刚完成分析(ID={last_beyond_window_event.id})，复用结果")
            log_ai_usage(
                route_type='reuse',
                alert_hash=last_beyond_window_event.alert_hash or '',
                source=last_beyond_window_event.source or ''
            )
            return last_beyond_window_event.ai_analysis or {}, False

        logger.debug(
            f"窗口外历史记录 ID={last_beyond_window_event.id} 已超过复用窗口({Config.RECENT_BEYOND_WINDOW_REUSE_SECONDS}s)，将尝试重新分析"
        )

    if original_event and not allow_reanalyze:
        logger.info(f"窗口外历史告警(ID={original_event.id})，复用历史分析结果")
        log_ai_usage(
            route_type='reuse',
            alert_hash=original_event.alert_hash or '',
            source=original_event.source or ''
        )
        return original_event.ai_analysis or {}, False

    if original_event:
        return await _analyze_now(webhook_full_data, f"窗口外历史告警(ID={original_event.id})，重新分析")

    return await _analyze_now(webhook_full_data, "窗口外历史告警缺少原始上下文，重新分析")


async def _resolve_analysis_with_lock(
    alert_hash: str,
    webhook_full_data: dict
) -> AnalysisResolution:
    duplicate_check = await run_in_threadpool(check_duplicate_alert,
        alert_hash,
        check_beyond_window=True
    )
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
            prefer_recent_beyond_window=False
        )
    elif is_duplicate and original_event:
        analysis_result, reanalyzed = await _resolve_duplicate_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data
        )
    else:
        analysis_result, reanalyzed = await _analyze_now(webhook_full_data, "新告警，开始 AI 分析...")

    return AnalysisResolution(analysis_result, reanalyzed, is_duplicate, original_event, beyond_window)


async def _resolve_analysis_without_lock(
    alert_hash: str,
    webhook_full_data: dict
) -> AnalysisResolution:
    logger.info(f"[Lock] 告警正在由其他节点处理，等待中: hash={alert_hash[:16]}")
    await asyncio.sleep(_LOCK_WAIT_SECONDS)

    duplicate_check = await run_in_threadpool(check_duplicate_alert,
        alert_hash,
        check_beyond_window=True
    )
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
            log_ai_usage(
                route_type='reuse',
                alert_hash=last_beyond_window_event.alert_hash or '',
                source=last_beyond_window_event.source or ''
            )
            analysis_result = last_beyond_window_event.ai_analysis or {}
            return AnalysisResolution(analysis_result, False, True, original_event, False)

    if beyond_window and original_event:
        if not last_beyond_window_event:
            logger.info(f"窗口外历史告警，等待其他worker完成处理: 历史 ID={original_event.id}")
            await asyncio.sleep(_LOCK_WAIT_SECONDS)
            duplicate_check = await run_in_threadpool(check_duplicate_alert,
                alert_hash,
                check_beyond_window=True
            )
            is_duplicate = duplicate_check.is_duplicate
            original_event = duplicate_check.original_event
            beyond_window = duplicate_check.beyond_window
            last_beyond_window_event = duplicate_check.last_beyond_window_event

        analysis_result, reanalyzed = await _resolve_beyond_window_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data,
            Config.REANALYZE_AFTER_TIME_WINDOW,
            prefer_recent_beyond_window=True
        )
    elif is_duplicate and original_event:
        analysis_result, reanalyzed = await _resolve_duplicate_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data
        )
    else:
        analysis_result, reanalyzed = await _analyze_now(webhook_full_data, "未找到已处理结果，重新处理...")

    return AnalysisResolution(analysis_result, reanalyzed, is_duplicate, original_event, beyond_window)


def _refresh_original_event(original_id: Optional[int], fallback_event: Optional[WebhookEvent]) -> Optional[WebhookEvent]:
    if not original_id:
        return fallback_event

    try:
        with get_session() as session:
            latest = session.get(WebhookEvent, original_id)
            return latest or fallback_event
    except Exception as e:
        logger.warning(f"重新查询原始告警失败: {e}")
        return fallback_event


def _recently_notified(original_event: Optional[WebhookEvent], original_id: Optional[int], alert_type: str) -> bool:
    if not original_event or not original_event.last_notified_at:
        return False

    seconds_since_notify = (datetime.now() - original_event.last_notified_at).total_seconds()
    if seconds_since_notify < Config.NOTIFICATION_COOLDOWN_SECONDS:
        logger.info(f"{alert_type}（原始 ID={original_id}），{seconds_since_notify:.1f}秒前已转发，跳过")
        return True

    return False


def _resolve_alert_type_label(is_duplicate: bool, beyond_window: bool, is_periodic_reminder: bool) -> str:
    if is_periodic_reminder:
        return '周期性提醒'
    if is_duplicate:
        return '窗口内重复'
    if beyond_window:
        return '窗口外重复'
    return '新'


def _decide_duplicate_forwarding(
    original_event: Optional[WebhookEvent],
    original_id: Optional[int]
) -> ForwardDecision:
    if _recently_notified(original_event, original_id, '窗口内重复告警'):
        return ForwardDecision(False, f'窗口内重复告警（原始 ID={original_id}），刚刚已转发', False)

    if Config.ENABLE_PERIODIC_REMINDER and original_event:
        last_notified = original_event.last_notified_at
        if last_notified:
            hours_since_notification = (datetime.now() - last_notified).total_seconds() / 3600
            if hours_since_notification >= Config.REMINDER_INTERVAL_HOURS:
                logger.info(
                    f"触发周期性提醒: 原始ID={original_id}, 距上次通知{hours_since_notification:.1f}小时, 已重复{original_event.duplicate_count}次"
                )
                return ForwardDecision(True, None, True)
            return ForwardDecision(False, f'窗口内重复告警（原始 ID={original_id}），距上次通知仅{hours_since_notification:.1f}小时', False)

    if not Config.FORWARD_DUPLICATE_ALERTS:
        return ForwardDecision(False, f'窗口内重复告警（原始 ID={original_id}），配置跳过转发', False)

    return ForwardDecision(True, None, False)


async def _resolve_analysis(alert_hash: str, webhook_full_data: dict, got_lock: bool) -> AnalysisResolution:
    if got_lock:
        return await _resolve_analysis_with_lock(alert_hash, webhook_full_data)
    return await _resolve_analysis_without_lock(alert_hash, webhook_full_data)


def _match_forward_rules(importance: str, is_duplicate: bool, beyond_window: bool, source: str) -> list:
    from core.models import ForwardRule

    try:
        with session_scope() as session:
            rules = session.query(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc()).all()

            if not rules:
                return []

            matched = []
            for rule in rules:
                if rule.match_importance:
                    allowed = [x.strip().lower() for x in rule.match_importance.split(',')]
                    if importance.lower() not in allowed:
                        continue

                if rule.match_duplicate and rule.match_duplicate != 'all':
                    if rule.match_duplicate == 'new' and (is_duplicate or beyond_window):
                        continue
                    if rule.match_duplicate == 'duplicate' and not is_duplicate:
                        continue
                    if rule.match_duplicate == 'beyond_window' and not beyond_window:
                        continue

                if rule.match_source:
                    allowed_sources = [x.strip().lower() for x in rule.match_source.split(',')]
                    if source.lower() not in allowed_sources:
                        continue

                matched.append(rule.to_dict())

                if rule.stop_on_match:
                    break

            return matched
    except Exception as e:
        logger.warning(f"加载转发规则失败: {e}")
        return []


def _decide_forwarding(
    importance: str,
    is_duplicate: bool,
    beyond_window: bool,
    noise_context: Optional[NoiseReductionContext],
    original_event: Optional[WebhookEvent],
    original_id: Optional[int],
    source: str = ''
) -> ForwardDecision:
    if noise_context and noise_context.suppress_forward:
        return ForwardDecision(
            False,
            f"智能降噪抑制转发: {noise_context.reason}",
            False,
        )

    matched_rules = _match_forward_rules(importance, is_duplicate, beyond_window, source)

    if matched_rules:
        if is_duplicate:
            dup_decision = _decide_duplicate_forwarding(original_event, original_id)
            if not dup_decision.should_forward:
                return ForwardDecision(False, dup_decision.skip_reason, False)
            return ForwardDecision(True, None, dup_decision.is_periodic_reminder, matched_rules)

        if beyond_window:
            if not Config.FORWARD_AFTER_TIME_WINDOW:
                return ForwardDecision(False, '窗口外重复告警，配置不转发', False)
            if _recently_notified(original_event, original_id, '窗口外重复告警'):
                return ForwardDecision(False, '近期已通知', False)
            return ForwardDecision(True, None, False, matched_rules)

        return ForwardDecision(True, None, False, matched_rules)

    if importance != 'high':
        return ForwardDecision(False, f'重要性为 {importance}，非高风险事件不自动转发', False)

    if beyond_window:
        if not Config.FORWARD_AFTER_TIME_WINDOW:
            return ForwardDecision(False, f'窗口外重复告警（原始 ID={original_id}），配置跳过转发', False)
        if _recently_notified(original_event, original_id, '窗口外重复告警'):
            return ForwardDecision(False, f'窗口外重复告警（原始 ID={original_id}），刚刚已转发', False)
        return ForwardDecision(True, None, False)

    if is_duplicate:
        return _decide_duplicate_forwarding(original_event, original_id)

    return ForwardDecision(True, None, False)


def _update_last_notified(event_id: int) -> None:
    try:
        from sqlalchemy import update

        with get_session() as session:
            session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id == event_id)
                .values(last_notified_at=datetime.now())
            )
            session.commit()
            logger.info(f"已更新原始告警 {event_id} 的 last_notified_at")
    except Exception as e:
        logger.warning(f"更新 last_notified_at 失败: {e}")


def _parse_webhook_request(client_ip: str, headers: dict, payload: dict, raw_body: bytes, source: Optional[str]) -> WebhookRequestContext:
    requested_source = source or headers.get('x-webhook-source', 'unknown')

    logger.info(f"[Webhook] 收到请求: IP={client_ip}, Source={requested_source}")
    try:
        import hashlib
        raw_hash = hashlib.sha256(raw_body).hexdigest() if raw_body else None
    except Exception:
        raw_hash = None
    logger.debug(f"[Webhook] 原始载荷: size={len(raw_body) if raw_body else 0}, sha256={raw_hash}")

    ensure_webhook_auth(headers, raw_body)

    if not payload and raw_body:
        import json
        try:
            payload = json.loads(raw_body)
        except Exception:
            raise InvalidJsonError()

    data = payload

    normalized = normalize_webhook_event(data, requested_source)
    parsed_data = normalized.data
    requested_source = normalized.source
    webhook_full_data = {
        'body': data,
        'headers': headers,
        'query': {},
        'parsed_data': parsed_data,
        'source': requested_source
    }

    return WebhookRequestContext(
        client_ip=client_ip,
        source=requested_source,
        payload=raw_body,
        parsed_data=parsed_data,
        webhook_full_data=webhook_full_data,
        headers=headers
    )


def _build_webhook_response(
    webhook_id: Union[int, str],
    analysis_result: dict,
    forward_result: dict,
    is_dup: bool,
    original_id: Optional[int],
    beyond_window: bool,
    is_within_window: bool
) -> JSONResponse:
    is_degraded = analysis_result.get('_degraded', False)
    degraded_reason = analysis_result.get('_degraded_reason')
    clean_analysis = {k: v for k, v in analysis_result.items() if not k.startswith('_')}

    return _ok(
        status=200,
        message='Webhook processed successfully',
        timestamp=datetime.now().isoformat(),
        webhook_id=webhook_id,
        ai_analysis=clean_analysis,
        ai_degraded=is_degraded,
        ai_degraded_reason=degraded_reason if is_degraded else None,
        forward_status=forward_result.get('status', 'unknown'),
        is_duplicate=is_dup,
        duplicate_of=original_id if is_dup else None,
        beyond_time_window=beyond_window,
        is_within_window=is_within_window
    )


async def handle_webhook_process(client_ip: str, headers: dict, payload: dict, raw_body: bytes, source: Optional[str] = None):
    logger.info(f"[Pipeline] 开始处理流程: source={source or 'unknown'}")
    WEBHOOK_RECEIVED_TOTAL.labels(source=source or 'unknown', status='received').inc()
    analysis_result = {}
    original_event = None

    try:
        try:
            request_context = _parse_webhook_request(client_ip, headers, payload, raw_body, source)
        except InvalidSignatureError:
            logger.warning(f"认证失败 (Token/Signature 不匹配或缺失): IP={client_ip}, Source={source or 'unknown'}")
            return
        except InvalidJsonError:
            return

        alert_hash = generate_alert_hash(request_context.parsed_data, request_context.source)

        async with processing_lock(alert_hash) as got_lock:
            logger.debug("[Pipeline] 进入 AI 分析阶段")
            analysis_resolution = await _resolve_analysis(alert_hash, request_context.webhook_full_data, got_lock)

            analysis_result = analysis_resolution.analysis_result
            original_event = analysis_resolution.original_event
            logger.debug("[Pipeline] 进入持久化与降噪计算阶段")
            persisted = await run_in_threadpool(_persist_webhook_with_noise_context,
                request_context=request_context,
                analysis_resolution=analysis_resolution,
                alert_hash=alert_hash)

            save_result = persisted.save_result
            noise_context = persisted.noise_context
            analysis_result = _apply_noise_metadata(analysis_result, noise_context)
            WEBHOOK_NOISE_REDUCED_TOTAL.labels(
                source=request_context.source,
                relation=noise_context.relation,
                suppressed=str(noise_context.suppress_forward).lower()
            ).inc()

        beyond_window = save_result.beyond_window
        is_dup = save_result.is_duplicate
        original_id = save_result.original_id
        is_duplicate = is_dup and not beyond_window
        importance = str(analysis_result.get('importance', '')).lower()

        original_event = await run_in_threadpool(_refresh_original_event, original_id, original_event)
        logger.debug("[Pipeline] 进入转发决策阶段")
        forward_decision = await run_in_threadpool(_decide_forwarding,
            importance,
            is_duplicate,
            beyond_window,
            noise_context,
            original_event,
            original_id,
            source=request_context.source)

        forward_result = {'status': 'skipped', 'reason': forward_decision.skip_reason}
        if forward_decision.should_forward:
            alert_type = await run_in_threadpool(_resolve_alert_type_label, is_duplicate, beyond_window, forward_decision.is_periodic_reminder)

            if forward_decision.matched_rules:
                from services.ai_analyzer import forward_to_openclaw
                forward_results = []
                for rule in forward_decision.matched_rules:
                    try:
                        logger.info(f"执行规则转发: {rule['name']} -> {rule['target_type']}")
                        if rule['target_type'] == 'openclaw':
                            result = await forward_to_openclaw(request_context.webhook_full_data, analysis_result)
                            if result.get('_pending') and result.get('run_id'):
                                try:
                                    with session_scope() as session:
                                        deep_record = DeepAnalysis(
                                            webhook_event_id=save_result.webhook_id,
                                            engine='openclaw',
                                            user_question='',
                                            analysis_result={
                                                'status': 'pending',
                                                'root_cause': 'OpenClaw Agent 正在分析中，结果将自动更新...',
                                                'impact': '分析已触发，预计几分钟内完成',
                                                'recommendations': ['结果将自动更新，请稍后刷新页面'],
                                                'confidence': 0
                                            },
                                            openclaw_run_id=result.get('run_id', ''),
                                            openclaw_session_key=result.get('session_key', ''),
                                            status='pending'
                                        )
                                        session.add(deep_record)
                                        session.flush()
                                        logger.info(f"转发分析记录已创建: id={deep_record.id}, run_id={result.get('run_id')}")
                                except Exception as e:
                                    logger.error(f"创建转发分析记录失败: {e}")
                        else:
                            result = await forward_to_remote(
                                request_context.webhook_full_data,
                                analysis_result,
                                target_url=rule['target_url'],
                                is_periodic_reminder=forward_decision.is_periodic_reminder
                            )
                        result['rule_name'] = rule['name']
                        forward_results.append(result)
                    except Exception as e:
                        logger.error(f"规则 {rule['name']} 转发失败: {e}")
                        forward_results.append({'status': 'error', 'rule_name': rule['name'], 'message': str(e)})

                forward_result = {'status': 'success', 'results': forward_results}
                if any(r.get('status') == 'success' for r in forward_results) and original_event:
                    await run_in_threadpool(_update_last_notified, original_event.id)
            else:
                logger.info(f"开始自动转发高风险{alert_type}告警...")
                forward_result = await forward_to_remote(request_context.webhook_full_data, analysis_result, is_periodic_reminder=forward_decision.is_periodic_reminder)
                if forward_result.get('status') == 'success' and original_event:
                    await run_in_threadpool(_update_last_notified, original_event.id)
        else:
            logger.info(f"跳过自动转发: {forward_decision.skip_reason}")

        logger.info(f"[Pipeline] 处理流程结束: id={save_result.webhook_id}, forwarded={forward_decision.should_forward}")
        return _build_webhook_response(
            save_result.webhook_id,
            analysis_result,
            forward_result,
            is_dup,
            original_id,
            beyond_window,
            is_duplicate
        )

    except Exception as e:
        logger.error(f"处理 Webhook 时发生错误: {str(e)}", exc_info=True)
        return
