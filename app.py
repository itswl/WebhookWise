import os
import time
import socket
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template, Response
from flask_compress import Compress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Generator, Union
from sqlalchemy.exc import IntegrityError

from pathlib import Path
from dotenv import dotenv_values
from config import Config
from logger import logger
from utils import (
    verify_signature, save_webhook_data, get_client_ip,
    get_all_webhooks, generate_alert_hash, check_duplicate_alert,
    SaveWebhookResult
)
from ai_analyzer import analyze_webhook_with_ai, forward_to_remote
from ecosystem_adapters import normalize_webhook_event
from alert_noise_reduction import AlertContext, analyze_noise_reduction
from models import WebhookEvent, ProcessingLock, session_scope, get_session, test_db_connection

app = Flask(__name__)
app.config.from_object(Config)

# 启用 gzip 压缩（减少响应体积，加快传输）
Compress(app)
app.config['COMPRESS_MIMETYPES'] = [
    'application/json',
    'text/html',
    'text/css',
    'application/javascript'
]
app.config['COMPRESS_LEVEL'] = 6  # 压缩级别 1-9（6是平衡值）
app.config['COMPRESS_MIN_SIZE'] = 500  # 超过500字节才压缩

# Worker 标识（用于调试）
_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

# 分布式锁配置
_LOCK_TTL_SECONDS = Config.PROCESSING_LOCK_TTL_SECONDS  # 锁过期时间（秒），防止崩溃后死锁
_LOCK_WAIT_SECONDS = Config.PROCESSING_LOCK_WAIT_SECONDS   # 等待锁的时间（秒）

# 响应工具函数：统一 success/error 返回结构
# 业务逻辑 helper：按“告警处理 / 配置管理 / 运维接口”分组


class WebhookRequestError(Exception):
    """基类：Webhook 请求解析错误。"""


class InvalidSignatureError(WebhookRequestError):
    """签名校验失败。"""


class InvalidJsonError(WebhookRequestError):
    """JSON 解析失败。"""


@dataclass(frozen=True)
class AnalysisResolution:
    analysis_result: dict
    reanalyzed: bool
    is_duplicate: bool
    original_event: Optional[WebhookEvent]
    beyond_window: bool


@dataclass(frozen=True)
class WebhookRequestContext:
    client_ip: str
    source: str
    payload: bytes
    parsed_data: dict
    webhook_full_data: dict


@dataclass(frozen=True)
class ForwardDecision:
    should_forward: bool
    skip_reason: Optional[str]
    is_periodic_reminder: bool


@dataclass(frozen=True)
class NoiseReductionContext:
    relation: str
    root_cause_event_id: Optional[int]
    confidence: float
    suppress_forward: bool
    reason: str
    related_alert_count: int
    related_alert_ids: list[int]


@dataclass(frozen=True)
class PersistedEventContext:
    save_result: SaveWebhookResult
    noise_context: NoiseReductionContext


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
    save_result = _persist_webhook_event(
        data=request_context.parsed_data,
        source=request_context.source,
        payload=request_context.payload,
        client_ip=request_context.client_ip,
        analysis_result=analysis_with_noise,
        alert_hash=alert_hash,
        is_duplicate=analysis_resolution.is_duplicate or analysis_resolution.beyond_window,
        original_event=analysis_resolution.original_event,
        beyond_window=analysis_resolution.beyond_window,
        reanalyzed=analysis_resolution.reanalyzed
    )

    return PersistedEventContext(save_result=save_result, noise_context=noise_context)


def _ok(data: Optional[dict] = None, http_status: int = 200, **extra):
    payload = {'success': True}
    if data is not None:
        payload['data'] = data
    payload.update(extra)
    return jsonify(payload), http_status


def _fail(error: str, http_status: int = 500, **extra):
    payload = {'success': False, 'error': error}
    payload.update(extra)
    return jsonify(payload), http_status


def _cleanup_expired_locks() -> int:
    """
    清理过期的处理锁（防止死锁）
    
    Returns:
        int: 清理的锁数量
    """
    try:
        session = get_session()
        try:
            threshold = datetime.now() - timedelta(seconds=_LOCK_TTL_SECONDS)
            deleted = session.query(ProcessingLock).filter(
                ProcessingLock.created_at < threshold
            ).delete()
            session.commit()
            if deleted > 0:
                logger.warning(f"清理了 {deleted} 个过期的处理锁")
            return deleted
        finally:
            session.close()
    except Exception as e:
        logger.error(f"清理过期锁失败: {e}")
        return 0


@contextmanager
def processing_lock(alert_hash: str) -> Generator[bool, None, None]:
    """
    告警处理锁上下文管理器（数据库级别分布式锁）
    
    利用数据库主键约束防止多 worker 并发处理同一告警。
    
    Yields:
        bool: True 表示成功获取锁，False 表示已有其他 worker 在处理
    """
    # 先清理过期锁
    _cleanup_expired_locks()
    
    session = get_session()
    lock_acquired = False
    
    try:
        # 尝试插入锁记录
        lock = ProcessingLock(
            alert_hash=alert_hash,
            created_at=datetime.now(),
            worker_id=_WORKER_ID
        )
        session.add(lock)
        session.commit()
        lock_acquired = True
        logger.debug(f"获取处理锁成功: hash={alert_hash[:16]}..., worker={_WORKER_ID}")
        yield True
        
    except IntegrityError:
        # 主键冲突，说明已有其他 worker 在处理
        session.rollback()
        logger.info(f"告警正由其他 worker 处理中: hash={alert_hash[:16]}...")
        yield False
        
    except Exception as e:
        session.rollback()
        logger.error(f"获取处理锁失败: {e}", exc_info=True)
        yield False
        
    finally:
        # 无论成功与否，都尝试释放锁
        if lock_acquired:
            try:
                session.query(ProcessingLock).filter(
                    ProcessingLock.alert_hash == alert_hash
                ).delete()
                session.commit()
                logger.debug(f"释放处理锁: hash={alert_hash[:16]}...")
            except Exception as e:
                logger.error(f"释放锁失败: {e}")
                session.rollback()
        session.close()


def _analyze_now(webhook_full_data: dict, message: str) -> tuple[dict, bool]:
    logger.info(message)
    return analyze_webhook_with_ai(webhook_full_data), True


def _resolve_duplicate_analysis(
    original_event: WebhookEvent,
    last_beyond_window_event: Optional[WebhookEvent],
    webhook_full_data: dict
) -> tuple[dict, bool]:
    if last_beyond_window_event and last_beyond_window_event.ai_analysis:
        logger.info(f"复用最近窗口外记录 ID={last_beyond_window_event.id} 的分析结果")
        return last_beyond_window_event.ai_analysis, False

    if original_event.ai_analysis:
        logger.info(f"复用原始告警 ID={original_event.id} 的分析结果")
        return original_event.ai_analysis, False

    return _analyze_now(webhook_full_data, f"原始告警 ID={original_event.id} 缺少AI分析，重新分析")


def _resolve_beyond_window_analysis(
    original_event: Optional[WebhookEvent],
    last_beyond_window_event: Optional[WebhookEvent],
    webhook_full_data: dict,
    allow_reanalyze: bool,
    prefer_recent_beyond_window: bool
) -> tuple[dict, bool]:
    if prefer_recent_beyond_window and last_beyond_window_event:
        logger.info(f"窗口外历史告警，复用最近窗口外记录 ID={last_beyond_window_event.id} 的分析结果")
        return last_beyond_window_event.ai_analysis or {}, False

    if original_event and not allow_reanalyze:
        logger.info(f"窗口外历史告警(ID={original_event.id})，复用历史分析结果")
        return original_event.ai_analysis or {}, False

    if original_event:
        return _analyze_now(webhook_full_data, f"窗口外历史告警(ID={original_event.id})，重新分析")

    return _analyze_now(webhook_full_data, "窗口外历史告警缺少原始上下文，重新分析")


def _resolve_analysis_with_lock(
    alert_hash: str,
    webhook_full_data: dict
) -> AnalysisResolution:
    """在成功获取处理锁后决定分析结果。"""
    duplicate_check = check_duplicate_alert(
        alert_hash,
        check_beyond_window=True
    )
    is_duplicate = duplicate_check.is_duplicate
    original_event = duplicate_check.original_event
    beyond_window = duplicate_check.beyond_window
    last_beyond_window_event = duplicate_check.last_beyond_window_event

    if beyond_window and original_event:
        analysis_result, reanalyzed = _resolve_beyond_window_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data,
            Config.REANALYZE_AFTER_TIME_WINDOW,
            prefer_recent_beyond_window=False
        )
    elif is_duplicate and original_event:
        analysis_result, reanalyzed = _resolve_duplicate_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data
        )
    else:
        analysis_result, reanalyzed = _analyze_now(webhook_full_data, "新告警，开始 AI 分析...")

    return AnalysisResolution(analysis_result, reanalyzed, is_duplicate, original_event, beyond_window)


def _resolve_analysis_without_lock(
    alert_hash: str,
    webhook_full_data: dict
) -> AnalysisResolution:
    """在处理锁被占用时决定分析结果（尽量复用其他 worker 的处理结果）。"""
    logger.info(f"等待其他 worker 处理完成: hash={alert_hash[:16]}...")
    time.sleep(_LOCK_WAIT_SECONDS)

    duplicate_check = check_duplicate_alert(
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
            analysis_result = last_beyond_window_event.ai_analysis or {}
            return AnalysisResolution(analysis_result, False, True, original_event, False)

    if beyond_window and original_event:
        if not last_beyond_window_event:
            logger.info(f"窗口外历史告警，等待其他worker完成处理: 历史 ID={original_event.id}")
            time.sleep(_LOCK_WAIT_SECONDS)
            duplicate_check = check_duplicate_alert(
                alert_hash,
                check_beyond_window=True
            )
            is_duplicate = duplicate_check.is_duplicate
            original_event = duplicate_check.original_event
            beyond_window = duplicate_check.beyond_window
            last_beyond_window_event = duplicate_check.last_beyond_window_event

        analysis_result, reanalyzed = _resolve_beyond_window_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data,
            Config.REANALYZE_AFTER_TIME_WINDOW,
            prefer_recent_beyond_window=True
        )
    elif is_duplicate and original_event:
        analysis_result, reanalyzed = _resolve_duplicate_analysis(
            original_event,
            last_beyond_window_event,
            webhook_full_data
        )
    else:
        analysis_result, reanalyzed = _analyze_now(webhook_full_data, "未找到已处理结果，重新处理...")

    return AnalysisResolution(analysis_result, reanalyzed, is_duplicate, original_event, beyond_window)


def _refresh_original_event(original_id: Optional[int], fallback_event: Optional[WebhookEvent]) -> Optional[WebhookEvent]:
    """读取数据库中的最新原始告警，避免并发场景使用过期对象。"""
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


def _resolve_analysis(alert_hash: str, webhook_full_data: dict, got_lock: bool) -> AnalysisResolution:
    if got_lock:
        return _resolve_analysis_with_lock(alert_hash, webhook_full_data)
    return _resolve_analysis_without_lock(alert_hash, webhook_full_data)


def _decide_forwarding(
    importance: str,
    is_duplicate: bool,
    beyond_window: bool,
    noise_context: Optional[NoiseReductionContext],
    original_event: Optional[WebhookEvent],
    original_id: Optional[int]
) -> ForwardDecision:
    """根据告警状态和配置决定是否自动转发。"""
    if noise_context and noise_context.suppress_forward:
        return ForwardDecision(
            False,
            f"智能降噪抑制转发: {noise_context.reason}",
            False,
        )

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
    """更新原始告警最近通知时间。"""
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



def _parse_webhook_request(source: Optional[str]) -> WebhookRequestContext:
    client_ip = get_client_ip(request)
    requested_source = source or request.headers.get('X-Webhook-Source', 'unknown')
    payload = request.get_data()

    logger.info(f"收到来自 {client_ip} 的 webhook 请求, 来源: {requested_source}")
    logger.debug(f"原始请求体: {payload.decode('utf-8', errors='ignore')[:500]}...")
    logger.debug(f"请求头: {dict(request.headers)}")

    signature = request.headers.get('X-Webhook-Signature', '')
    if signature and not verify_signature(payload, signature):
        raise InvalidSignatureError()

    try:
        data = request.get_json()
    except Exception as e:
        logger.error(f"JSON 解析失败: {str(e)}")
        raise InvalidJsonError() from e

    normalized = normalize_webhook_event(data, requested_source, request.headers)
    resolved_source = normalized.source
    data = normalized.data
    if normalized.adapter != 'passthrough':
        logger.info(f"生态适配命中: adapter={normalized.adapter}, source={resolved_source}")

    webhook_full_data = {
        'source': resolved_source,
        'parsed_data': data,
        'timestamp': datetime.now().isoformat(),
        'client_ip': client_ip
    }
    return WebhookRequestContext(client_ip, resolved_source, payload, data, webhook_full_data)


def _persist_webhook_event(
    *,
    data: dict,
    source: str,
    payload: bytes,
    client_ip: str,
    analysis_result: dict,
    alert_hash: str,
    is_duplicate: bool,
    original_event: Optional[WebhookEvent],
    beyond_window: bool,
    reanalyzed: bool
) -> SaveWebhookResult:
    return save_webhook_data(
        data=data,
        source=source,
        raw_payload=payload,
        headers=request.headers,
        client_ip=client_ip,
        ai_analysis=analysis_result,
        forward_status='pending',
        alert_hash=alert_hash,
        is_duplicate=is_duplicate,
        original_event=original_event,
        beyond_window=beyond_window,
        reanalyzed=reanalyzed
    )


def _build_webhook_response(
    webhook_id: Union[int, str],
    analysis_result: dict,
    forward_result: dict,
    is_dup: bool,
    original_id: Optional[int],
    beyond_window: bool,
    is_within_window: bool
) -> tuple[Response, int]:
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


def handle_webhook_process(source: Optional[str] = None) -> tuple[Response, int]:
    """通用 Webhook 处理逻辑"""
    analysis_result = {}
    original_event = None

    try:
        try:
            request_context = _parse_webhook_request(source)
        except InvalidSignatureError:
            logger.warning(f"签名验证失败: IP={get_client_ip(request)}, Source={source or 'unknown'}")
            return _fail('Invalid signature', 401)
        except InvalidJsonError:
            return _fail('Invalid JSON payload', 400)

        alert_hash = generate_alert_hash(request_context.parsed_data, request_context.source)

        with processing_lock(alert_hash) as got_lock:
            analysis_resolution = _resolve_analysis(alert_hash, request_context.webhook_full_data, got_lock)

            analysis_result = analysis_resolution.analysis_result
            original_event = analysis_resolution.original_event
            persisted = _persist_webhook_with_noise_context(
                request_context=request_context,
                analysis_resolution=analysis_resolution,
                alert_hash=alert_hash,
            )

            save_result = persisted.save_result
            noise_context = persisted.noise_context
            analysis_result = _apply_noise_metadata(analysis_result, noise_context)

        beyond_window = save_result.beyond_window
        is_dup = save_result.is_duplicate
        original_id = save_result.original_id
        is_duplicate = is_dup and not beyond_window
        importance = str(analysis_result.get('importance', '')).lower()

        original_event = _refresh_original_event(original_id, original_event)
        forward_decision = _decide_forwarding(
            importance,
            is_duplicate,
            beyond_window,
            noise_context,
            original_event,
            original_id
        )

        forward_result = {'status': 'skipped', 'reason': forward_decision.skip_reason}
        if forward_decision.should_forward:
            alert_type = _resolve_alert_type_label(is_duplicate, beyond_window, forward_decision.is_periodic_reminder)
            logger.info(f"开始自动转发高风险{alert_type}告警...")
            forward_result = forward_to_remote(request_context.webhook_full_data, analysis_result, is_periodic_reminder=forward_decision.is_periodic_reminder)

            if forward_result.get('status') == 'success' and original_event:
                _update_last_notified(original_event.id)
        else:
            logger.info(f"跳过自动转发: {forward_decision.skip_reason}")

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
        return _fail('Internal server error', 500)


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    return _ok(status=200, service_status='healthy', timestamp=datetime.now().isoformat(), service='webhook-receiver')


@app.route('/', methods=['GET'])
def dashboard():
    """Webhook 数据展示页面"""
    return render_template('dashboard.html')


@app.route('/api/webhooks', methods=['GET'])
def list_webhooks() -> tuple[Response, int]:
    """获取 webhook 列表 API（支持游标分页和字段选择）"""
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    cursor_id = request.args.get('cursor', None, type=int)  # 游标分页
    fields = request.args.get('fields', 'summary')  # 字段选择：summary | full

    # 限制每页最大数量（根据数据量调整）
    if fields == 'full':
        page_size = min(page_size, 50)  # 完整数据限制更严格
    else:
        page_size = min(page_size, 200)  # 摘要数据可以返回更多

    webhooks, total, next_cursor = get_all_webhooks(
        page=page, page_size=page_size, cursor_id=cursor_id, fields=fields
    )

    return _ok(
        status=200,
        data=webhooks,
        pagination={
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size if total > 0 else 0,
            'next_cursor': next_cursor  # 游标分页支持
        }
    )


@app.route('/api/webhooks/<int:webhook_id>', methods=['GET'])
def get_webhook_detail(webhook_id: int) -> tuple[Response, int]:
    """获取单条 webhook 详细信息（完整数据）"""
    try:
        with session_scope() as session:
            event = session.query(WebhookEvent).filter_by(id=webhook_id).first()
            if not event:
                return _fail('Webhook not found', 404)

            # 转换为字典
            data = event.to_dict()

            # 添加上次告警 ID（同一 hash 的上一条记录）
            if event.alert_hash:
                try:
                    prev_alert = session.query(WebhookEvent)\
                        .filter(
                            WebhookEvent.alert_hash == event.alert_hash,
                            WebhookEvent.timestamp < event.timestamp
                        )\
                        .order_by(WebhookEvent.timestamp.desc())\
                        .first()

                    data['prev_alert_id'] = prev_alert.id if prev_alert else None
                    data['prev_alert_timestamp'] = prev_alert.timestamp.isoformat() if prev_alert else None
                    logger.info(f"计算 prev_alert_id: webhook={event.id}, prev_alert_id={data['prev_alert_id']}, hash={event.alert_hash[:16]}...")
                except Exception as e:
                    logger.warning(f"计算 prev_alert_id 失败 (webhook={event.id}): {e}")
                    data['prev_alert_id'] = None
                    data['prev_alert_timestamp'] = None
            else:
                data['prev_alert_id'] = None
                data['prev_alert_timestamp'] = None
                logger.warning(f"webhook {event.id} 没有 alert_hash，无法计算 prev_alert_id")

            return _ok(data, 200)
    except Exception as e:
        logger.error(f"查询 webhook 详情失败: {str(e)}")
        return _fail(str(e), 500)

# 配置管理 Schema: key -> (env_var, value_type, validator)
_CONFIG_SCHEMA = {
    'forward_url': ('FORWARD_URL', 'str', lambda x: x.startswith('http')),
    'enable_forward': ('ENABLE_FORWARD', 'bool', None),
    'enable_ai_analysis': ('ENABLE_AI_ANALYSIS', 'bool', None),
    'openai_api_key': ('OPENAI_API_KEY', 'str', None),
    'openai_api_url': ('OPENAI_API_URL', 'str', lambda x: x.startswith('http')),
    'openai_model': ('OPENAI_MODEL', 'str', lambda x: len(x) > 0),
    'ai_system_prompt': ('AI_SYSTEM_PROMPT', 'str', None),
    'log_level': ('LOG_LEVEL', 'str', lambda x: x.upper() in ['DEBUG', 'INFO', 'WARNING', 'ERROR']),
    'duplicate_alert_time_window': ('DUPLICATE_ALERT_TIME_WINDOW', 'int', lambda x: 1 <= x <= 168),
    'forward_duplicate_alerts': ('FORWARD_DUPLICATE_ALERTS', 'bool', None),
    'reanalyze_after_time_window': ('REANALYZE_AFTER_TIME_WINDOW', 'bool', None),
    'forward_after_time_window': ('FORWARD_AFTER_TIME_WINDOW', 'bool', None),
    'enable_alert_noise_reduction': ('ENABLE_ALERT_NOISE_REDUCTION', 'bool', None),
    'noise_reduction_window_minutes': ('NOISE_REDUCTION_WINDOW_MINUTES', 'int', lambda x: 1 <= x <= 60),
    'root_cause_min_confidence': ('ROOT_CAUSE_MIN_CONFIDENCE', 'float', lambda x: 0 <= x <= 1),
    'suppress_derived_alert_forward': ('SUPPRESS_DERIVED_ALERT_FORWARD', 'bool', None)
}


def _load_env_values(env_path: str = '.env') -> dict:
    path = Path(env_path)
    if not path.exists():
        return {}
    return dict(dotenv_values(path))


def _coerce_config_value(value, value_type: str, default=None):
    if value_type == 'bool':
        if isinstance(value, str):
            return value.lower() == 'true'
        return bool(value)
    if value_type == 'int':
        return int(value) if value not in (None, '') else default
    if value_type == 'float':
        return float(value) if value not in (None, '') else default
    return value


def _resolve_config_value(env_values: dict, key: str, default=None, value_type: str = 'str'):
    value = env_values.get(key)
    if value is None:
        value = getattr(Config, key, default)
    return _coerce_config_value(value, value_type, default)


def _build_config_response(env_values: dict) -> dict:
    api_key = _resolve_config_value(env_values, 'OPENAI_API_KEY', '')
    masked_key = '已配置' if api_key else '未配置'

    return {
        'forward_url': _resolve_config_value(env_values, 'FORWARD_URL', ''),
        'enable_forward': _resolve_config_value(env_values, 'ENABLE_FORWARD', False, 'bool'),
        'enable_ai_analysis': _resolve_config_value(env_values, 'ENABLE_AI_ANALYSIS', True, 'bool'),
        'openai_api_key': masked_key,
        'openai_api_url': _resolve_config_value(env_values, 'OPENAI_API_URL', 'https://openrouter.ai/api/v1'),
        'openai_model': _resolve_config_value(env_values, 'OPENAI_MODEL', 'anthropic/claude-sonnet-4'),
        'ai_system_prompt': _resolve_config_value(env_values, 'AI_SYSTEM_PROMPT', Config.AI_SYSTEM_PROMPT),
        'log_level': _resolve_config_value(env_values, 'LOG_LEVEL', 'INFO'),
        'duplicate_alert_time_window': _resolve_config_value(env_values, 'DUPLICATE_ALERT_TIME_WINDOW', 24, 'int'),
        'forward_duplicate_alerts': _resolve_config_value(env_values, 'FORWARD_DUPLICATE_ALERTS', False, 'bool'),
        'reanalyze_after_time_window': _resolve_config_value(env_values, 'REANALYZE_AFTER_TIME_WINDOW', True, 'bool'),
        'forward_after_time_window': _resolve_config_value(env_values, 'FORWARD_AFTER_TIME_WINDOW', True, 'bool'),
        'enable_alert_noise_reduction': _resolve_config_value(env_values, 'ENABLE_ALERT_NOISE_REDUCTION', True, 'bool'),
        'noise_reduction_window_minutes': _resolve_config_value(env_values, 'NOISE_REDUCTION_WINDOW_MINUTES', 5, 'int'),
        'root_cause_min_confidence': _resolve_config_value(env_values, 'ROOT_CAUSE_MIN_CONFIDENCE', 0.65, 'float'),
        'suppress_derived_alert_forward': _resolve_config_value(env_values, 'SUPPRESS_DERIVED_ALERT_FORWARD', True, 'bool')
    }


def _parse_update_value(key: str, raw_value, value_type: str, validator):
    if value_type == 'bool':
        if isinstance(raw_value, bool):
            typed_value = raw_value
        elif isinstance(raw_value, str):
            typed_value = raw_value.lower() == 'true'
        else:
            raise ValueError(f"{key} 应为布尔类型")
        return str(typed_value).lower(), typed_value

    if value_type == 'int':
        typed_value = int(raw_value)
        if validator and not validator(typed_value):
            raise ValueError(f"{key} 值超出有效范围")
        return str(typed_value), typed_value

    if value_type == 'float':
        typed_value = float(raw_value)
        if validator and not validator(typed_value):
            raise ValueError(f"{key} 值超出有效范围")
        return str(typed_value), typed_value

    typed_value = str(raw_value).strip()
    if not typed_value:
        return None, None
    if validator and not validator(typed_value):
        raise ValueError(f"{key} 格式无效")
    return typed_value, typed_value


def _collect_config_updates(payload: dict) -> tuple[dict, list[str]]:
    updates = {}
    errors = []

    for key, raw_value in payload.items():
        if key not in _CONFIG_SCHEMA:
            continue

        env_var, value_type, validator = _CONFIG_SCHEMA[key]
        try:
            string_value, typed_value = _parse_update_value(key, raw_value, value_type, validator)
            if string_value is None:
                logger.debug(f"跳过空值配置: {key}")
                continue
            updates[env_var] = (string_value, typed_value)
        except ValueError as e:
            errors.append(str(e))

    return updates, errors


def _merge_env_lines(lines: list[str], updates: dict) -> list[str]:
    updated_vars = set()
    merged = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            merged.append(line)
            continue

        if '=' not in stripped:
            merged.append(line)
            continue

        var_name = stripped.split('=', 1)[0].strip()
        if var_name in updates:
            new_value, _ = updates[var_name]
            merged.append(f'{var_name}={new_value}\n')
            updated_vars.add(var_name)
        else:
            merged.append(line)

    for var_name, (string_value, _) in updates.items():
        if var_name not in updated_vars:
            merged.append(f'{var_name}={string_value}\n')

    return merged


def _persist_config_updates(updates: dict, env_file: str = '.env') -> None:
    env_path = Path(env_file)
    lines = env_path.read_text(encoding='utf-8').splitlines(keepends=True) if env_path.exists() else []
    merged_lines = _merge_env_lines(lines, updates)

    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(merged_lines)
        f.flush()

    for var_name, (_, typed_value) in updates.items():
        setattr(Config, var_name, typed_value)
        os.environ[var_name] = str(typed_value).lower() if isinstance(typed_value, bool) else str(typed_value)


@app.route('/api/config', methods=['GET'])
def get_config():
    """获取当前配置（从 .env 文件实时读取）"""
    try:
        env_values = _load_env_values('.env')
        return _ok(_build_config_response(env_values), 200)
    except Exception as e:
        logger.error(f"获取配置失败: {str(e)}")
        return _fail(str(e), 500)


@app.route('/api/config', methods=['POST'])
def update_config():
    """更新配置"""
    try:
        payload = request.get_json()
        if not payload:
            return _fail('请求体为空', 400)

        updates, errors = _collect_config_updates(payload)
        if errors:
            return _fail('; '.join(errors), 400)

        try:
            _persist_config_updates(updates, '.env')
        except PermissionError as e:
            logger.error(f"权限错误，无法写入 .env 文件: {str(e)}")
            return _fail('权限错误: 无法写入配置文件。请检查 .env 文件权限或使用环境变量配置。', 500)
        except Exception as e:
            logger.error(f"更新 .env 文件失败: {str(e)}", exc_info=True)
            raise

        logger.info(f"配置已更新: {list(updates.keys())}")
        return _ok(status=200, message='配置更新成功')

    except Exception as e:
        logger.error(f"更新配置失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)



def _get_webhook_event_by_id(session, webhook_id: int) -> Optional[WebhookEvent]:
    return session.query(WebhookEvent).filter_by(id=webhook_id).first()


def _build_webhook_context(event: WebhookEvent) -> dict:
    return {
        'source': event.source,
        'parsed_data': event.parsed_data,
        'timestamp': event.timestamp.isoformat() if event.timestamp else None,
        'client_ip': event.client_ip
    }


def _propagate_analysis_to_duplicates(session, webhook_id: int, analysis_result: dict, new_importance: Optional[str]) -> int:
    duplicate_events = session.query(WebhookEvent).filter(WebhookEvent.duplicate_of == webhook_id).all()
    for dup in duplicate_events:
        dup.ai_analysis = analysis_result
        dup.importance = new_importance
    return len(duplicate_events)


def _reanalyze_webhook_event(session, webhook_event: WebhookEvent, webhook_id: int) -> tuple[dict, Optional[str], Optional[str], int]:
    webhook_data = _build_webhook_context(webhook_event)

    logger.info(f"重新分析 webhook ID: {webhook_id}")
    analysis_result = analyze_webhook_with_ai(webhook_data)

    old_importance = webhook_event.importance
    new_importance = analysis_result.get('importance')

    webhook_event.ai_analysis = analysis_result
    webhook_event.importance = new_importance

    logger.info(f"重新分析完成: {old_importance} → {new_importance} - {analysis_result.get('summary', '')}")

    updated_duplicates = 0
    if webhook_event.is_duplicate == 0:
        updated_duplicates = _propagate_analysis_to_duplicates(session, webhook_id, analysis_result, new_importance)
        if updated_duplicates:
            logger.info(f"同时更新了 {updated_duplicates} 条重复告警的分析结果")

    return analysis_result, old_importance, new_importance, updated_duplicates


@app.route('/api/reanalyze/<int:webhook_id>', methods=['POST'])
def reanalyze_webhook(webhook_id: int) -> tuple[Response, int]:
    """重新分析指定的 webhook，并更新所有引用它的重复告警"""
    try:
        with session_scope() as session:
            webhook_event = _get_webhook_event_by_id(session, webhook_id)
            if not webhook_event:
                return _fail('Webhook not found', 404)

            analysis_result, old_importance, new_importance, updated_duplicates = _reanalyze_webhook_event(
                session,
                webhook_event,
                webhook_id
            )

            return _ok(
                status=200,
                analysis=analysis_result,
                original_importance=old_importance,
                new_importance=new_importance,
                updated_duplicates=updated_duplicates,
                message=f'重新分析完成，importance: {old_importance} → {new_importance}' +
                        (f'，同时更新了 {updated_duplicates} 条重复告警' if updated_duplicates > 0 else '')
            )

    except Exception as e:
        logger.error(f"重新分析失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)

def _manual_forward(session, webhook_event: WebhookEvent, webhook_id: int, custom_url: Optional[str]) -> dict:
    webhook_data = _build_webhook_context(webhook_event)
    analysis_result = webhook_event.ai_analysis or {}

    logger.info(f"手动转发 webhook ID: {webhook_id} 到 {custom_url or Config.FORWARD_URL}")
    forward_result = forward_to_remote(webhook_data, analysis_result, custom_url)

    webhook_event.forward_status = forward_result.get('status', 'unknown')
    return forward_result


@app.route('/api/forward/<int:webhook_id>', methods=['POST'])
def manual_forward_webhook(webhook_id: int) -> tuple[Response, int]:
    """手动转发指定的 webhook"""
    try:
        with session_scope() as session:
            webhook_event = _get_webhook_event_by_id(session, webhook_id)
            if not webhook_event:
                return _fail('Webhook not found', 404)

            request_data = request.get_json(silent=True) or {}
            custom_url = request_data.get('forward_url')
            forward_result = _manual_forward(session, webhook_event, webhook_id, custom_url)

            return _ok(
                status=200,
                success=forward_result.get('status') == 'success',
                result=forward_result,
                message='Forward completed'
            )

    except Exception as e:
        logger.error(f"手动转发失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)



def _build_prompt_source() -> str:
    if Config.AI_USER_PROMPT:
        return 'environment'
    if Config.AI_USER_PROMPT_FILE:
        return 'file'
    return 'default'


def _reload_prompt_template() -> str:
    from ai_analyzer import reload_user_prompt_template

    new_template = reload_user_prompt_template()
    logger.info("AI Prompt 模板已重新加载")
    return new_template


def _load_current_prompt_template() -> str:
    from ai_analyzer import load_user_prompt_template

    return load_user_prompt_template()


def _run_add_unique_constraint_migration() -> bool:
    from migrations_tool import add_unique_constraint

    logger.info("开始执行数据库迁移：添加唯一约束")
    return add_unique_constraint()


@app.route('/api/prompt/reload', methods=['POST'])
def reload_prompt() -> tuple[Response, int]:
    """重新加载 AI Prompt 模板"""
    try:
        new_template = _reload_prompt_template()
        return _ok(
            status=200,
            message='Prompt 模板已重新加载',
            template_length=len(new_template),
            preview=new_template[:200] + '...' if len(new_template) > 200 else new_template
        )
    except Exception as e:
        logger.error(f"重新加载 prompt 模板失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)


@app.route('/api/prompt', methods=['GET'])
def get_prompt() -> tuple[Response, int]:
    """获取当前 AI Prompt 模板"""
    try:
        template = _load_current_prompt_template()
        return _ok(
            status=200,
            template=template,
            source=_build_prompt_source()
        )
    except Exception as e:
        logger.error(f"获取 prompt 模板失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)


@app.route('/webhook', methods=['POST'])
def receive_webhook():
    """接收通用 Webhook 接口"""
    return handle_webhook_process()


@app.route('/webhook/<source>', methods=['POST'])
def receive_webhook_with_source(source):
    """接收指定来源的 Webhook 接口"""
    return handle_webhook_process(source)


@app.errorhandler(404)
def not_found(error):
    """404 错误处理"""
    return _fail('Endpoint not found', 404)


@app.errorhandler(405)
def method_not_allowed(error):
    """405 错误处理"""
    return _fail('Method not allowed', 405)



@app.route('/api/migrations/add_unique_constraint', methods=['POST'])
def migration_add_unique_constraint() -> tuple[Response, int]:
    """执行数据库迁移：添加唯一约束"""
    try:
        success = _run_add_unique_constraint_migration()
        if success:
            return _ok(status=200, message='数据库迁移成功：唯一约束已添加')

        return _fail('数据库迁移失败，请查看日志', 500)

    except Exception as e:
        logger.error(f"执行迁移失败: {e}")
        return _fail(str(e), 500)

if __name__ == '__main__':
    # 启动前验证
    Config.validate()
    if not test_db_connection():
        logger.error("数据库连接失败，请检查配置")
    
    logger.info(f"启动 Webhook 服务: http://{Config.HOST}:{Config.PORT}")
    app.run(
        host=Config.HOST,
        port=Config.PORT,
        debug=Config.DEBUG
    )
