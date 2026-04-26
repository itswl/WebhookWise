from contextlib import contextmanager, asynccontextmanager
from pathlib import Path
import hmac
import hashlib
import json
import os
import time
import threading
import httpx
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional, Union, Generator, AsyncGenerator

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.config import Config
from core.logger import logger
from core.models import WebhookEvent, get_session, session_scope

WebhookData = dict[str, Any]


class CircuitState(Enum):
    CLOSED = "closed"      # 正常，允许请求通过
    OPEN = "open"          # 熔断，拒绝所有请求
    HALF_OPEN = "half_open"  # 半开，允许试探请求


class CircuitBreaker:
    """
    熔断器实现，防止级联故障。

    - CLOSED（正常）：请求通过，失败计数；达到阈值后转为 OPEN
    - OPEN（熔断）：请求直接拒绝（返回 None），超时后转为 HALF_OPEN
    - HALF_OPEN（半开）：允许一个试探请求；成功则回 CLOSED，失败则回 OPEN
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple = (httpx.RequestError,),
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions

        self._lock = threading.RLock()
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._state = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if (
                    self._last_failure_time is not None
                    and time.time() - self._last_failure_time >= self.recovery_timeout
                ):
                    self._state = CircuitState.HALF_OPEN
            return self._state

    def call(self, func: Callable, *args, **kwargs):
        """执行函数，失败时触发熔断。"""
        # threshold 为 0 表示禁用熔断器，直接执行
        if self.failure_threshold == 0:
            try:
                return func(*args, **kwargs)
            except self.expected_exceptions as e:
                logger.warning(f"CircuitBreaker [{self.name}] 请求异常（已禁用）: {e}")
                return None
        
        if self.state == CircuitState.OPEN:
            logger.warning(f"CircuitBreaker [{self.name}] OPEN — 请求被拒绝")
            return None

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exceptions as e:
            self._on_failure()
            logger.warning(f"CircuitBreaker [{self.name}] 请求异常: {e}")
            return None

    async def call_async(self, func: Callable, *args, **kwargs):
        """异步执行函数，失败时触发熔断。"""
        if self.failure_threshold == 0:
            try:
                return await func(*args, **kwargs)
            except self.expected_exceptions as e:
                logger.warning(f"CircuitBreaker [{self.name}] 请求异常（已禁用）: {e}")
                return None
        
        if self.state == CircuitState.OPEN:
            logger.warning(f"CircuitBreaker [{self.name}] OPEN — 请求被拒绝")
            return None

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exceptions as e:
            self._on_failure()
            logger.warning(f"CircuitBreaker [{self.name}] 请求异常: {e}")
            raise

    def _on_success(self):
        with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def _on_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            # threshold 为 0 表示禁用熔断器
            if self.failure_threshold > 0 and self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.error(f"CircuitBreaker [{self.name}] 转为 OPEN（连续 {self._failure_count} 次失败）")


# 预置熔断器实例（通过 Config）

feishu_cb = CircuitBreaker(name="feishu", failure_threshold=Config.CIRCUIT_BREAKER_FEISHU_THRESHOLD, recovery_timeout=Config.CIRCUIT_BREAKER_FEISHU_TIMEOUT)
openclaw_cb = CircuitBreaker(name="openclaw", failure_threshold=Config.CIRCUIT_BREAKER_OPENCLAW_THRESHOLD, recovery_timeout=Config.CIRCUIT_BREAKER_OPENCLAW_TIMEOUT)
forward_cb = CircuitBreaker(name="forward", failure_threshold=Config.CIRCUIT_BREAKER_FORWARD_THRESHOLD, recovery_timeout=Config.CIRCUIT_BREAKER_FORWARD_TIMEOUT)


HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]


@dataclass(frozen=True)
class DuplicateCheckResult:
    is_duplicate: bool
    original_event: Optional[WebhookEvent]
    beyond_window: bool
    last_beyond_window_event: Optional[WebhookEvent]


@dataclass(frozen=True)
class SaveWebhookResult:
    webhook_id: Union[int, str]
    is_duplicate: bool
    original_id: Optional[int]
    beyond_window: bool


MAX_SAVE_RETRIES = Config.SAVE_MAX_RETRIES
RETRY_DELAY_SECONDS = Config.SAVE_RETRY_DELAY_SECONDS


def verify_signature(payload: bytes, signature: str, secret: Optional[str] = None) -> bool:
    """验证 webhook 签名"""
    if secret is None:
        secret = Config.WEBHOOK_SECRET
    
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    result = hmac.compare_digest(expected_signature, signature)
    if not result:
        logger.warning(f"[Auth] 签名比对不匹配: expected_prefix={expected_signature[:8]}, actual_prefix={signature[:8]}")
    else:
        logger.debug("[Auth] 签名验证通过")
    return result


# ====== 告警哈希字段配置 ======
# Prometheus Alertmanager 格式的字段提取配置
PROMETHEUS_ROOT_FIELDS = ['alertingRuleName']
PROMETHEUS_LABEL_FIELDS = [
    'alertname', 'internal_label_alert_level',
    'host', 'instance', 'pod', 'namespace', 'service', 'path', 'method'
]
PROMETHEUS_ALERT_FIELDS = ['fingerprint']

# 华为云/通用告警格式的字段提取配置
GENERIC_FIELDS = [
    'Type', 'RuleName', 'event', 'event_type',
    'MetricName', 'Level', 'alert_id', 'alert_name', 'resource_id', 'service'
]



def _extract_fields(data: dict[str, Any], fields: list[str], lower_keys: bool = True) -> dict[str, Any]:
    """从字典中提取指定字段。"""
    extracted = {}
    for field in fields:
        if field in data:
            key = field.lower() if lower_keys else field
            extracted[key] = data[field]
    return extracted


def _extract_prometheus_fields(data: dict[str, Any]) -> dict[str, Any]:
    """提取 Prometheus Alertmanager 格式的关键字段。"""
    key_fields = _extract_fields(data, PROMETHEUS_ROOT_FIELDS)

    alerts = data.get('alerts', [])
    first_alert = alerts[0] if alerts and isinstance(alerts[0], dict) else None
    if not first_alert:
        return key_fields

    labels = first_alert.get('labels', {})
    if isinstance(labels, dict):
        key_fields.update(_extract_fields(labels, PROMETHEUS_LABEL_FIELDS, lower_keys=False))

    key_fields.update(_extract_fields(first_alert, PROMETHEUS_ALERT_FIELDS, lower_keys=False))
    return key_fields


def _extract_generic_fields(data: dict[str, Any]) -> dict[str, Any]:
    """提取华为云/通用告警格式的关键字段。"""
    key_fields = _extract_fields(data, GENERIC_FIELDS)

    resources = data.get('Resources', [])
    first_resource = (
        resources[0]
        if isinstance(resources, list) and resources and isinstance(resources[0], dict)
        else None
    )
    if not first_resource:
        return key_fields

    resource_id = first_resource.get('InstanceId') or first_resource.get('Id') or first_resource.get('id')
    if resource_id:
        key_fields['resource_id'] = resource_id

    dimensions = first_resource.get('Dimensions', [])
    if not isinstance(dimensions, list):
        return key_fields

    important_dims = {'Node', 'ResourceID', 'Instance', 'InstanceId', 'Host', 'Pod', 'Container'}
    for dim in dimensions:
        if not isinstance(dim, dict):
            continue
        dim_name = dim.get('Name', '')
        dim_value = dim.get('Value')
        if dim_name in important_dims and dim_value:
            key_fields[f'dim_{dim_name.lower()}'] = dim_value

    return key_fields


def generate_alert_hash(data: dict[str, Any], source: str) -> str:
    """
    生成告警的唯一哈希值，用于识别重复告警

    Args:
        data: webhook 数据
        source: 数据来源

    Returns:
        str: SHA256 哈希值
    """
    key_fields = {'source': source}

    if isinstance(data, dict):
        is_prometheus = (
            'alerts' in data and
            isinstance(data.get('alerts'), list) and
            len(data['alerts']) > 0
        )

        if is_prometheus:
            key_fields.update(_extract_prometheus_fields(data))
        else:
            key_fields.update(_extract_generic_fields(data))

    key_string = json.dumps(key_fields, sort_keys=True, ensure_ascii=False)
    hash_value = hashlib.sha256(key_string.encode('utf-8')).hexdigest()

    logger.debug(f"[Hash] 生成告警哈希: hash={hash_value[:16]}..., input_keys={list(key_fields.keys())}")
    return hash_value


def _query_last_beyond_window_event(session: Session, alert_hash: str) -> Optional[WebhookEvent]:
    return (
        session.query(WebhookEvent)
        .filter(
            WebhookEvent.alert_hash == alert_hash,
            WebhookEvent.beyond_window == 1
        )
        .order_by(WebhookEvent.timestamp.desc())
        .first()
    )


def _query_latest_original_event(session: Session, alert_hash: str) -> Optional[WebhookEvent]:
    return (
        session.query(WebhookEvent)
        .filter(
            WebhookEvent.alert_hash == alert_hash,
            WebhookEvent.is_duplicate == 0
        )
        .order_by(WebhookEvent.timestamp.desc())
        .first()
    )


def _find_recent_window_event(
    session: Session,
    alert_hash: str,
    time_threshold: datetime
) -> Optional[WebhookEvent]:
    return (
        session.query(WebhookEvent)
        .filter(
            WebhookEvent.alert_hash == alert_hash,
            WebhookEvent.timestamp >= time_threshold
        )
        .order_by(WebhookEvent.timestamp.desc())
        .first()
    )


def _resolve_window_start(
    original_ref: WebhookEvent,
    last_beyond_window: Optional[WebhookEvent]
) -> tuple[datetime, int]:
    if last_beyond_window:
        logger.debug(f"找到窗口外记录作为起点: ID={last_beyond_window.id}, 时间={last_beyond_window.timestamp}")
        return last_beyond_window.timestamp, last_beyond_window.id

    logger.debug(f"使用原始告警作为起点: ID={original_ref.id}, 时间={original_ref.timestamp}")
    return original_ref.timestamp, original_ref.id


def _resolve_original_reference(session: Session, any_event: WebhookEvent) -> WebhookEvent:
    original_id = any_event.duplicate_of if any_event.is_duplicate else any_event.id
    if not original_id:
        return any_event
    return session.get(WebhookEvent, original_id) or any_event


def check_duplicate_alert(
    alert_hash: str,
    time_window_hours: Optional[int] = None,
    session: Optional[Session] = None,
    check_beyond_window: bool = False
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
        time_window_hours = Config.DUPLICATE_ALERT_TIME_WINDOW

    should_close = session is None
    if should_close:
        session = get_session()

    now = datetime.now()

    try:
        time_threshold = now - timedelta(hours=time_window_hours)

        # 先查窗口内最新记录，保证同一时间窗口内只产生一条“原始上下文”。
        # 这样在并发写入时，后续请求可以稳定复用同一条分析结果。
        any_event = _find_recent_window_event(session, alert_hash, time_threshold)

        if any_event:
            original_ref = _resolve_original_reference(session, any_event)
            original_id = original_ref.id
            last_beyond_window = _query_last_beyond_window_event(session, alert_hash)

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
            last_beyond_window = _query_last_beyond_window_event(session, alert_hash)
            history_event = _query_latest_original_event(session, alert_hash)

            if history_event:
                time_diff = (now - history_event.timestamp).total_seconds() / 3600
                logger.info(
                    f"窗口外发现历史告警: hash={alert_hash}, "
                    f"原始告警ID={history_event.id}, 时间差={time_diff:.1f}小时"
                )
                # 返回历史原始事件与 recent beyond_window，交给上层做“复用或重算”决策。
                return DuplicateCheckResult(False, history_event, True, last_beyond_window)

        return DuplicateCheckResult(False, None, False, None)

    except Exception as e:
        logger.error(f"检查重复告警失败: {str(e)}")
        return DuplicateCheckResult(False, None, False, None)
    finally:
        if should_close:
            session.close()




def _decode_raw_payload(raw_payload: Optional[bytes]) -> Optional[str]:
    return raw_payload.decode('utf-8') if raw_payload else None


def _normalize_headers(headers: Optional[HeadersDict]) -> HeadersDict:
    return dict(headers) if headers else {}


def _resolve_analysis_for_duplicate(
    ai_analysis: Optional[AnalysisResult],
    original: WebhookEvent,
    reanalyzed: bool
) -> tuple[AnalysisResult, Optional[str]]:
    if ai_analysis:
        final_analysis = ai_analysis
        final_importance = ai_analysis.get('importance')
    elif original.ai_analysis:
        final_analysis = original.ai_analysis
        final_importance = original.importance
    else:
        final_analysis = {}
        final_importance = None

    if ai_analysis and reanalyzed and (not original.ai_analysis or not original.ai_analysis.get('summary')):
        logger.info(f"更新原始告警 ID={original.id} 的AI分析结果（之前缺失）")
        original.ai_analysis = ai_analysis
        original.importance = ai_analysis.get('importance')

    return final_analysis, final_importance


def _build_event(
    *,
    source: str,
    client_ip: Optional[str],
    raw_payload: Optional[bytes],
    headers: Optional[HeadersDict],
    data: WebhookData,
    alert_hash: str,
    ai_analysis: Optional[AnalysisResult],
    importance: Optional[str],
    forward_status: str,
    is_duplicate: int,
    duplicate_of: Optional[int],
    duplicate_count: int,
    beyond_window: int,
    last_notified_at: Optional[datetime] = None
) -> WebhookEvent:
    return WebhookEvent(
        source=source,
        client_ip=client_ip,
        timestamp=datetime.now(),
        raw_payload=_decode_raw_payload(raw_payload),
        headers=_normalize_headers(headers),
        parsed_data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=importance,
        forward_status=forward_status,
        is_duplicate=is_duplicate,
        duplicate_of=duplicate_of,
        duplicate_count=duplicate_count,
        beyond_window=beyond_window,
        last_notified_at=last_notified_at
    )


def _save_duplicate_event(
    session: Session,
    *,
    source: str,
    client_ip: Optional[str],
    raw_payload: Optional[bytes],
    headers: Optional[HeadersDict],
    data: WebhookData,
    alert_hash: str,
    ai_analysis: Optional[AnalysisResult],
    forward_status: str,
    original_event: WebhookEvent,
    beyond_window: bool,
    reanalyzed: bool
) -> Optional[SaveWebhookResult]:
    original = session.get(WebhookEvent, original_event.id)
    if not original:
        return None

    original.duplicate_count = (original.duplicate_count or 1) + 1
    original.updated_at = datetime.now()
    logger.info(f"发现重复告警，原始告警ID={original.id}, 已重复{original.duplicate_count}次")

    final_ai_analysis, final_importance = _resolve_analysis_for_duplicate(ai_analysis, original, reanalyzed)
    duplicate_event = _build_event(
        source=source,
        client_ip=client_ip,
        raw_payload=raw_payload,
        headers=headers,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=final_ai_analysis,
        importance=final_importance,
        forward_status=forward_status,
        is_duplicate=1,
        duplicate_of=original.id,
        duplicate_count=original.duplicate_count,
        beyond_window=1 if beyond_window else 0
    )

    session.add(duplicate_event)
    session.flush()

    if ai_analysis:
        logger.info(f"重复告警已保存: ID={duplicate_event.id}, 使用传入的AI分析结果")
    elif original.ai_analysis:
        logger.info(
            f"重复告警已保存: ID={duplicate_event.id}, "
            f"复用原始告警 {original.id} 的AI分析结果"
        )
    else:
        logger.info(f"重复告警已保存: ID={duplicate_event.id}, 无AI分析结果")

    if Config.ENABLE_FILE_BACKUP:
        save_webhook_to_file(data, source, raw_payload, headers, client_ip, final_ai_analysis)

    return SaveWebhookResult(duplicate_event.id, True, original.id, beyond_window)


def _save_new_event(
    session: Session,
    *,
    source: str,
    client_ip: Optional[str],
    raw_payload: Optional[bytes],
    headers: Optional[HeadersDict],
    data: WebhookData,
    alert_hash: str,
    ai_analysis: Optional[AnalysisResult],
    forward_status: str
) -> SaveWebhookResult:
    webhook_event = _build_event(
        source=source,
        client_ip=client_ip,
        raw_payload=raw_payload,
        headers=headers,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=ai_analysis.get('importance') if ai_analysis else None,
        forward_status=forward_status,
        is_duplicate=0,
        duplicate_of=None,
        duplicate_count=1,
        beyond_window=0,
        last_notified_at=datetime.now()
    )

    session.add(webhook_event)
    session.flush()
    logger.info(f"Webhook 数据已保存到数据库: ID={webhook_event.id}")

    if Config.ENABLE_FILE_BACKUP:
        save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)

    return SaveWebhookResult(webhook_event.id, False, None, False)


def _save_to_file_fallback(
    data: WebhookData,
    source: str,
    raw_payload: Optional[bytes],
    headers: Optional[HeadersDict],
    client_ip: Optional[str],
    ai_analysis: Optional[AnalysisResult]
) -> SaveWebhookResult:
    file_id = save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)
    return SaveWebhookResult(file_id, False, None, False)


def save_webhook_data(
    data: WebhookData,
    source: str = 'unknown',
    raw_payload: Optional[bytes] = None,
    headers: Optional[HeadersDict] = None,
    client_ip: Optional[str] = None,
    ai_analysis: Optional[AnalysisResult] = None,
    forward_status: str = 'pending',
    alert_hash: Optional[str] = None,
    is_duplicate: Optional[bool] = None,
    original_event: Optional[WebhookEvent] = None,
    beyond_window: bool = False,
    reanalyzed: bool = False
) -> SaveWebhookResult:
    """保存 webhook 数据到数据库（带重试机制防止并发竞态）。"""
    if alert_hash is None:
        alert_hash = generate_alert_hash(data, source)

    for attempt in range(MAX_SAVE_RETRIES):
        try:
            with session_scope() as session:
                # 在同一事务内重新判重，避免外层结果在高并发下过期。
                if is_duplicate is None:
                    duplicate_check = check_duplicate_alert(
                        alert_hash,
                        session=session
                    )
                    is_duplicate = duplicate_check.is_duplicate
                    original_event = duplicate_check.original_event
                    beyond_window = duplicate_check.beyond_window

                if is_duplicate and original_event:
                    saved = _save_duplicate_event(
                        session,
                        source=source,
                        client_ip=client_ip,
                        raw_payload=raw_payload,
                        headers=headers,
                        data=data,
                        alert_hash=alert_hash,
                        ai_analysis=ai_analysis,
                        forward_status=forward_status,
                        original_event=original_event,
                        beyond_window=beyond_window,
                        reanalyzed=reanalyzed
                    )
                    if saved:
                        return saved

                return _save_new_event(
                    session,
                    source=source,
                    client_ip=client_ip,
                    raw_payload=raw_payload,
                    headers=headers,
                    data=data,
                    alert_hash=alert_hash,
                    ai_analysis=ai_analysis,
                    forward_status=forward_status
                )

        except IntegrityError as e:
            logger.warning(f"检测到并发插入冲突 (attempt {attempt + 1}/{MAX_SAVE_RETRIES}): {str(e)}")

            if attempt < MAX_SAVE_RETRIES - 1:
                # 指数退避让并发写入先完成，再次判重时更容易命中已落库记录。
                time.sleep(RETRY_DELAY_SECONDS * (2 ** attempt))
                is_duplicate = None
                original_event = None
                logger.info(f"正在重试... (attempt {attempt + 2}/{MAX_SAVE_RETRIES})")
                continue

            # 最后兜底：直接读最新原始告警并降级写入重复记录，避免请求彻底失败。
            logger.error(f"重试 {MAX_SAVE_RETRIES} 次后仍然失败，尝试最后查找")
            with session_scope() as fallback_session:
                existing = _query_latest_original_event(fallback_session, alert_hash)

                if not existing:
                    logger.error(f"并发冲突但无法找到原始告警: hash={alert_hash}")
                    raise

                logger.info(f"最终找到原始告警 ID={existing.id}，标记为重复")
                existing.duplicate_count += 1

                final_ai_analysis = ai_analysis if ai_analysis else existing.ai_analysis
                final_importance = ai_analysis.get('importance') if ai_analysis else existing.importance

                dup_event = _build_event(
                    source=source,
                    client_ip=client_ip,
                    raw_payload=raw_payload,
                    headers=headers,
                    data=data,
                    alert_hash=alert_hash,
                    ai_analysis=final_ai_analysis,
                    importance=final_importance,
                    forward_status=forward_status,
                    is_duplicate=1,
                    duplicate_of=existing.id,
                    duplicate_count=existing.duplicate_count,
                    beyond_window=1 if beyond_window else 0
                )
                fallback_session.add(dup_event)
                fallback_session.flush()
                return SaveWebhookResult(dup_event.id, True, existing.id, beyond_window)

        except Exception as e:
            logger.error(f"保存 webhook 数据到数据库失败: {str(e)}")
            return _save_to_file_fallback(data, source, raw_payload, headers, client_ip, ai_analysis)

    logger.error("保存数据异常：退出重试循环但未返回结果")
    return _save_to_file_fallback(data, source, raw_payload, headers, client_ip, ai_analysis)


def save_webhook_to_file(
    data: WebhookData,
    source: str = 'unknown',
    raw_payload: Optional[bytes] = None,
    headers: Optional[HeadersDict] = None,
    client_ip: Optional[str] = None,
    ai_analysis: Optional[AnalysisResult] = None
) -> str:
    """保存 webhook 数据到文件(备份方式)"""
    # 创建数据目录
    os.makedirs(Config.DATA_DIR, exist_ok=True)
    
    # 生成文件名(基于时间戳)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    filename = f"{source}_{timestamp}.json"
    filepath = str(Path(Config.DATA_DIR) / filename)
    
    # 准备保存的完整数据
    full_data = {
        'timestamp': datetime.now().isoformat(),
        'source': source,
        'client_ip': client_ip,
        'headers': dict(headers) if headers else {},
        'raw_payload': raw_payload.decode('utf-8') if raw_payload else None,
        'parsed_data': data
    }
    
    # 添加 AI 分析结果
    if ai_analysis:
        full_data['ai_analysis'] = ai_analysis
    
    # 保存数据
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(full_data, f, indent=2, ensure_ascii=False)
    
    return filepath


def get_client_ip(request: Request) -> str:
    """获取客户端 IP 地址"""
    forwarded_for = request.headers.get('x-forwarded-for')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()

    real_ip = request.headers.get('x-real-ip')
    if real_ip:
        return real_ip

    return request.client.host if request.client else 'unknown'


def get_all_webhooks(
    page: int = 1,
    page_size: int = 20,
    cursor_id: Optional[int] = None,
    fields: str = 'summary'
) -> tuple[list[dict], int, Optional[int]]:
    """
    从数据库获取 webhook 数据（支持游标分页和字段选择）

    Args:
        page: 页码（仅用于首次加载或无游标时）
        page_size: 每页数量
        cursor_id: 游标 ID，获取此 ID 之后的数据（更高效）
        fields: 字段选择 - 'summary'(摘要), 'full'(完整)

    Returns:
        tuple: (webhook数据列表, 总数量, 下一页游标ID)
    """
    try:
        with session_scope() as session:
            # 查询总数
            total = session.query(WebhookEvent).count()

            # 构建查询
            query = session.query(WebhookEvent)

            # 筛选条件
            if cursor_id is not None:
                # 游标分页：获取 ID 小于 cursor_id 的记录（因为按 ID 降序）
                query = query.filter(WebhookEvent.id < cursor_id)

            # 先排序（必须在 offset 和 limit 之前）
            query = query.order_by(WebhookEvent.id.desc())

            # 再分页
            if cursor_id is None:
                # 无游标时使用 offset（仅首次加载）
                offset = (page - 1) * page_size
                if offset > 0:
                    query = query.offset(offset)

            # 最后限制数量
            events = query.limit(page_size).all()

            # 根据 fields 参数决定返回哪些字段
            if fields == 'summary':
                # 摘要模式：只返回列表必需的字段，减少数据传输量
                webhooks = [event.to_summary_dict() for event in events]
            else:
                # 完整模式：返回所有字段
                webhooks = [event.to_dict() for event in events]

            # 为重复告警添加窗口信息和上次告警 ID（批量计算优化）
            # 直接从数据库字段读取，无需动态计算
            for webhook in webhooks:
                # beyond_window 已经在数据库中固化，直接使用
                beyond_window = bool(webhook.get('beyond_window', 0))
                webhook['beyond_time_window'] = beyond_window
                webhook['is_within_window'] = not beyond_window if webhook.get('is_duplicate') else False

            # 批量计算上次告警 ID（优化性能）
            # 收集所有需要查询的 (hash, timestamp)
            lookup_map = {}
            for webhook in webhooks:
                if webhook.get('alert_hash'):
                    try:
                        current_timestamp = datetime.fromisoformat(webhook['timestamp'])
                        key = (webhook['alert_hash'], current_timestamp)
                        lookup_map[key] = webhook
                    except Exception as e:
                        logger.warning(f"解析时间戳失败 (webhook={webhook.get('id')}): {e}")
                        webhook['prev_alert_id'] = None

            # 批量查询所有的上一条记录（一次查询）
            if lookup_map:
                try:
                    # 获取所有涉及的 alert_hash
                    all_hashes = list(set(k[0] for k in lookup_map.keys()))

                    # 查询这些 hash 的所有记录（去重需要）
                    all_alerts = session.query(WebhookEvent.id, WebhookEvent.alert_hash, WebhookEvent.timestamp)\
                        .filter(WebhookEvent.alert_hash.in_(all_hashes))\
                        .order_by(WebhookEvent.alert_hash, WebhookEvent.timestamp.desc())\
                        .all()

                    # 构建 hash -> 按时间排序的记录列表
                    hash_to_alerts = {}
                    for alert_id, alert_hash, alert_timestamp in all_alerts:
                        if alert_hash not in hash_to_alerts:
                            hash_to_alerts[alert_hash] = []
                        hash_to_alerts[alert_hash].append((alert_id, alert_timestamp))

                    # 为每个 webhook 找到上一条记录
                    for (alert_hash, current_timestamp), webhook in lookup_map.items():
                        alerts_list = hash_to_alerts.get(alert_hash, [])
                        # 找到时间早于当前的第一条
                        prev_id = None
                        prev_timestamp = None
                        for aid, ats in alerts_list:
                            if ats < current_timestamp:
                                prev_id = aid
                                prev_timestamp = ats
                                break
                        webhook['prev_alert_id'] = prev_id
                        webhook['prev_alert_timestamp'] = prev_timestamp.isoformat() if prev_timestamp else None
                except Exception as e:
                    logger.warning(f"批量计算 prev_alert_id 失败: {e}")
                    # 失败时设置为 None
                    for webhook in lookup_map.values():
                        webhook['prev_alert_id'] = None
                        webhook['prev_alert_timestamp'] = None

            # 没有 alert_hash 的设为 None
            for webhook in webhooks:
                if not webhook.get('alert_hash'):
                    webhook['prev_alert_id'] = None
                    webhook['prev_alert_timestamp'] = None

            # 计算下一页游标
            next_cursor = events[-1].id if events else None

            return webhooks, total, next_cursor

    except Exception as e:
        logger.error(f"从数据库查询 webhook 数据失败: {str(e)}")
        webhooks = get_webhooks_from_files(limit=page_size)
        return webhooks, len(webhooks), None


def get_webhooks_from_files(limit: int = 50) -> list[dict]:
    """从文件获取 webhook 数据(备份方式)"""
    if not os.path.exists(Config.DATA_DIR):
        return []
    
    webhooks = []
    files = [f for f in os.listdir(Config.DATA_DIR) if f.endswith('.json')]
    
    # 读取所有文件
    for filename in files:
        filepath = str(Path(Config.DATA_DIR) / filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                webhook_data = json.load(f)
                webhook_data['filename'] = filename
                webhooks.append(webhook_data)
        except Exception as e:
            logger.error(f"读取文件失败 {filename}: {str(e)}")
    
    # 按 timestamp 字段倒序排序（最新的在前面）
    webhooks.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    
    # 返回限制数量的结果
    return webhooks[:limit]


@asynccontextmanager
async def processing_lock(alert_hash: str) -> AsyncGenerator[bool, None]:
    """
    告警处理锁上下文管理器（Redis 分布式锁）
    
    利用 Redis SET NX EX 防止多 worker 并发处理同一告警。
    """
    import core.redis_client
    redis_client = core.redis_client.get_redis()
    lock_key = f"lock:webhook:{alert_hash}"
    lock_value = Config.WORKER_ID
    
    lock_acquired = False
    
    try:
        # 尝试获取锁
        lock_acquired = bool(redis_client.set(lock_key, lock_value, nx=True, ex=Config.PROCESSING_LOCK_TTL_SECONDS))
        if lock_acquired:
            logger.debug(f"[Lock] 成功锁定告警: hash={alert_hash}, worker={Config.WORKER_ID}")
        else:
            logger.debug(f"告警正由其他 worker 处理中: hash={alert_hash[:16]}...")
    except Exception as e:
        logger.error(f"获取处理锁失败: {e}")

    try:
        yield lock_acquired
    finally:
        if lock_acquired:
            try:
                release_lua = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                else
                    return 0
                end
                """
                redis_client.eval(release_lua, 1, lock_key, lock_value)
                logger.debug(f"释放处理锁: hash={alert_hash[:16]}...")
            except Exception as e:
                logger.error(f"释放锁失败: {e}")
