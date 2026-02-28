import hmac
import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional, Union

from config import Config
from logger import logger
from models import WebhookEvent, get_session, session_scope

# 类型别名
WebhookData = dict[str, Any]
HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]


def verify_signature(payload: bytes, signature: str, secret: Optional[str] = None) -> bool:
    """验证 webhook 签名"""
    if secret is None:
        secret = Config.WEBHOOK_SECRET
    
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected_signature, signature)


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


def _extract_fields(data: dict, fields: list, prefix: str = '') -> dict:
    """从数据中提取指定字段"""
    result = {}
    for field in fields:
        if field in data:
            key = f"{prefix}{field}" if prefix else field.lower().replace('_', '')
            result[key] = data[field]
    return result


def _extract_prometheus_fields(data: dict) -> dict:
    """提取 Prometheus Alertmanager 格式的关键字段"""
    key_fields = {}
    
    # 提取根级字段
    for field in PROMETHEUS_ROOT_FIELDS:
        if field in data:
            key_fields[field.lower()] = data[field]
    
    # 提取第一个告警的字段
    alerts = data.get('alerts', [])
    if alerts and isinstance(alerts[0], dict):
        first_alert = alerts[0]
        
        # 提取标签字段
        labels = first_alert.get('labels', {})
        if isinstance(labels, dict):
            for field in PROMETHEUS_LABEL_FIELDS:
                if field in labels:
                    key_fields[field] = labels[field]
        
        # 提取告警级别字段
        for field in PROMETHEUS_ALERT_FIELDS:
            if field in first_alert:
                key_fields[field] = first_alert[field]
    
    return key_fields


def _extract_generic_fields(data: dict) -> dict:
    """提取华为云/通用告警格式的关键字段"""
    key_fields = {}

    # 提取通用字段
    for field in GENERIC_FIELDS:
        if field in data:
            key_fields[field.lower()] = data[field]

    # 特殊处理: Resources 字段
    resources = data.get('Resources', [])
    if isinstance(resources, list) and resources:
        first_resource = resources[0]
        if isinstance(first_resource, dict):
            # 提取资源 ID (优先级: InstanceId > Id > id)
            resource_id = first_resource.get('InstanceId') or first_resource.get('Id') or first_resource.get('id')
            if resource_id:
                key_fields['resource_id'] = resource_id

            # 提取 Dimensions 中的关键字段（如 Node、ResourceID 等）
            dimensions = first_resource.get('Dimensions', [])
            if isinstance(dimensions, list):
                for dim in dimensions:
                    if isinstance(dim, dict):
                        dim_name = dim.get('Name', '')
                        dim_value = dim.get('Value')

                        # 提取重要的维度信息
                        if dim_name and dim_value:
                            # 将维度名称标准化为小写，添加到关键字段
                            # 特别关注: Node (节点)、ResourceID (资源ID)、Instance (实例) 等
                            if dim_name in ['Node', 'ResourceID', 'Instance', 'InstanceId', 'Host', 'Pod', 'Container']:
                                key_fields[f'dim_{dim_name.lower()}'] = dim_value

    return key_fields


def generate_alert_hash(data: dict, source: str) -> str:
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
        # 检测告警格式并提取字段
        is_prometheus = (
            'alerts' in data and 
            isinstance(data.get('alerts'), list) and 
            len(data['alerts']) > 0
        )
        
        if is_prometheus:
            key_fields.update(_extract_prometheus_fields(data))
        else:
            key_fields.update(_extract_generic_fields(data))
    
    # 生成稳定的 JSON 字符串（排序键确保一致性）
    key_string = json.dumps(key_fields, sort_keys=True, ensure_ascii=False)
    
    # 计算 SHA256 哈希
    hash_value = hashlib.sha256(key_string.encode('utf-8')).hexdigest()
    
    logger.debug(f"生成告警哈希: {hash_value}, 关键字段: {key_fields}")
    return hash_value


def check_duplicate_alert(
    alert_hash: str,
    time_window_hours: Optional[int] = None,
    session = None
) -> tuple[bool, Optional[WebhookEvent]]:
    """
    检查是否存在重复告警

    Args:
        alert_hash: 告警哈希值
        time_window_hours: 时间窗口（小时）
        session: 数据库会话（如果提供，使用现有事务；否则创建新会话）

    Returns:
        (is_duplicate, original_event)
    """
    if not alert_hash:
        return False, None

    # 使用配置文件中的时间窗口设置
    if time_window_hours is None:
        time_window_hours = Config.DUPLICATE_ALERT_TIME_WINDOW

    # 如果没有提供session，创建新的
    should_close = session is None
    if should_close:
        session = get_session()

    try:
        # 计算时间窗口的起始时间
        time_threshold = datetime.now() - timedelta(hours=time_window_hours)

        # 查询相同哈希值的告警（时间窗口内，且不是重复告警）
        # 使用 with_for_update() 添加行锁，防止并发竞态
        # 注意：不使用 skip_locked，而是等待锁释放，确保读取到最新数据
        original_event = session.query(WebhookEvent)\
            .filter(
                WebhookEvent.alert_hash == alert_hash,
                WebhookEvent.timestamp >= time_threshold,
                WebhookEvent.is_duplicate == 0  # 只查找原始告警
            )\
            .order_by(WebhookEvent.timestamp.desc())\
            .with_for_update(nowait=False)\ # 等待锁释放，而不是跳过
            .first()

        if original_event:
            logger.info(f"检测到重复告警: hash={alert_hash}, 原始告警ID={original_event.id}, 时间窗口={time_window_hours}小时")
            return True, original_event
        else:
            return False, None

    except Exception as e:
        logger.error(f"检查重复告警失败: {str(e)}")
        return False, None
    finally:
        if should_close:
            session.close()


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
    original_event: Optional[WebhookEvent] = None
) -> tuple[Union[int, str], bool, Optional[int]]:
    """保存 webhook 数据到数据库（带重试机制防止并发竞态）"""
    from sqlalchemy.exc import IntegrityError

    # 如果未提供预计算的哈希值，则重新计算
    if alert_hash is None:
        alert_hash = generate_alert_hash(data, source)

    # 重试次数（用于处理并发竞态）
    max_retries = 3
    retry_delay = 0.1  # 100ms

    for attempt in range(max_retries):
        try:
            with session_scope() as session:
                # 在事务内检查重复（如果未预检测）
                if is_duplicate is None:
                    is_duplicate, original_event = check_duplicate_alert(alert_hash, session=session)
            if is_duplicate and original_event:
                # 重复告警：使用 session.get() 更高效地获取原始告警并更新重复计数
                orig = session.get(WebhookEvent, original_event.id)
                if orig:
                    orig.duplicate_count = (orig.duplicate_count or 1) + 1
                    orig.updated_at = datetime.now()
                    
                    logger.info(f"发现重复告警，原始告警ID={orig.id}, 已重复{orig.duplicate_count}次")
                    
                    # 创建重复告警记录（复用传入的 original_event 数据，避免重复读取）
                    webhook_event = WebhookEvent(
                        source=source,
                        client_ip=client_ip,
                        timestamp=datetime.now(),
                        raw_payload=raw_payload.decode('utf-8') if raw_payload else None,
                        headers=dict(headers) if headers else {},
                        parsed_data=data,
                        alert_hash=alert_hash,
                        ai_analysis=original_event.ai_analysis,  # 使用传入的数据
                        importance=original_event.importance,    # 使用传入的数据
                        forward_status=forward_status,
                        is_duplicate=1,
                        duplicate_of=original_event.id,
                        duplicate_count=1
                    )
                    
                    session.add(webhook_event)
                    session.flush()  # 获取 ID
                    
                    webhook_id = webhook_event.id
                    logger.info(f"重复告警已保存: ID={webhook_id}, 复用原始告警{orig.id}的AI分析结果")
                    
                    # 可选: 同时保存到文件
                    if Config.ENABLE_FILE_BACKUP:
                        save_webhook_to_file(data, source, raw_payload, headers, client_ip, orig.ai_analysis)
                    
                    return webhook_id, True, orig.id
            
                # 新告警：正常保存
                webhook_event = WebhookEvent(
                    source=source,
                    client_ip=client_ip,
                    timestamp=datetime.now(),
                    raw_payload=raw_payload.decode('utf-8') if raw_payload else None,
                    headers=dict(headers) if headers else {},
                    parsed_data=data,
                    alert_hash=alert_hash,
                    ai_analysis=ai_analysis,
                    importance=ai_analysis.get('importance') if ai_analysis else None,
                    forward_status=forward_status,
                    is_duplicate=0,
                    duplicate_of=None,
                    duplicate_count=1
                )

                session.add(webhook_event)
                session.flush()  # 获取 ID

                webhook_id = webhook_event.id
                logger.info(f"Webhook 数据已保存到数据库: ID={webhook_id}")

                # 可选: 同时保存到文件
                if Config.ENABLE_FILE_BACKUP:
                    save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)

                return webhook_id, False, None

        except IntegrityError as e:
            # 唯一约束冲突：说明另一个 worker 已经插入了相同的原始告警
            logger.warning(f"检测到并发插入冲突 (attempt {attempt + 1}/{max_retries}): {str(e)}")

            if attempt < max_retries - 1:
                # 重试：等待一小段时间后重新检查
                import time
                time.sleep(retry_delay * (attempt + 1))  # 指数退避
                is_duplicate = None  # 重置状态，强制重新检查
                original_event = None
                logger.info(f"正在重试... (attempt {attempt + 2}/{max_retries})")
                continue
            else:
                # 最后一次重试失败，尝试最后一次查找
                logger.error(f"重试 {max_retries} 次后仍然失败，尝试最后查找")
                from sqlalchemy import text
                with session_scope() as fallback_session:
                    # 直接查询（不加锁）
                    existing = fallback_session.query(WebhookEvent)\
                        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.is_duplicate == 0)\
                        .order_by(WebhookEvent.timestamp.desc())\
                        .first()

                    if existing:
                        # 找到了，标记为重复
                        logger.info(f"最终找到原始告警 ID={existing.id}，标记为重复")
                        existing.duplicate_count += 1

                        dup_event = WebhookEvent(
                            source=source,
                            client_ip=client_ip,
                            timestamp=datetime.now(),
                            raw_payload=raw_payload.decode('utf-8') if raw_payload else None,
                            headers=dict(headers) if headers else {},
                            parsed_data=data,
                            alert_hash=alert_hash,
                            ai_analysis=existing.ai_analysis,
                            importance=existing.importance,
                            forward_status=forward_status,
                            is_duplicate=1,
                            duplicate_of=existing.id,
                            duplicate_count=1
                        )
                        fallback_session.add(dup_event)
                        fallback_session.flush()

                        return dup_event.id, True, existing.id
                    else:
                        # 真的没找到，记录错误
                        logger.error(f"并发冲突但无法找到原始告警: hash={alert_hash}")
                        raise

        except Exception as e:
            logger.error(f"保存 webhook 数据到数据库失败: {str(e)}")
            # 失败时至少保存到文件
            file_id = save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)
            return file_id, False, None

    # 不应该执行到这里
    logger.error("保存数据异常：退出重试循环但未返回结果")
    file_id = save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)
    return file_id, False, None


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
    if not os.path.exists(Config.DATA_DIR):
        os.makedirs(Config.DATA_DIR)
    
    # 生成文件名(基于时间戳)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    filename = f"{source}_{timestamp}.json"
    filepath = os.path.join(Config.DATA_DIR, filename)
    
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


def get_client_ip(request) -> str:
    """获取客户端 IP 地址"""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    elif request.headers.get('X-Real-IP'):
        return request.headers.get('X-Real-IP')
    else:
        return request.remote_addr


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
        filepath = os.path.join(Config.DATA_DIR, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                data['filename'] = filename
                webhooks.append(data)
        except Exception as e:
            logger.error(f"读取文件失败 {filename}: {str(e)}")
    
    # 按 timestamp 字段倒序排序（最新的在前面）
    webhooks.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    
    # 返回限制数量的结果
    return webhooks[:limit]
