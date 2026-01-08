import hmac
import hashlib
import json
import os
from datetime import datetime, timedelta
from config import Config
from logger import logger
from models import WebhookEvent, get_session


def verify_signature(payload, signature, secret=None):
    """
    验证 webhook 签名
    
    Args:
        payload: 请求体数据
        signature: 请求头中的签名
        secret: 密钥(可选,默认使用配置中的密钥)
    
    Returns:
        bool: 签名是否有效
    """
    if secret is None:
        secret = Config.WEBHOOK_SECRET
    
    # 计算期望的签名
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    # 比较签名(防止时序攻击)
    return hmac.compare_digest(expected_signature, signature)


def generate_alert_hash(data, source):
    """
    生成告警的唯一哈希值，用于识别重复告警
    
    Args:
        data: webhook 数据
        source: 数据来源
    
    Returns:
        str: SHA256 哈希值
    """
    # 提取关键字段用于生成唯一标识
    # 根据不同的告警来源，提取不同的关键字段
    key_fields = {
        'source': source,
    }
    
    # 云监控告警特定字段
    if isinstance(data, dict):
        # 告警类型和规则名称
        if 'Type' in data:
            key_fields['type'] = data.get('Type')
        if 'RuleName' in data:
            key_fields['rule_name'] = data.get('RuleName')
        if 'event' in data:
            key_fields['event'] = data.get('event')
        if 'event_type' in data:
            key_fields['event_type'] = data.get('event_type')
        
        # 资源标识
        if 'Resources' in data:
            resources = data.get('Resources', [])
            if isinstance(resources, list) and len(resources) > 0:
                # 提取第一个资源的标识
                first_resource = resources[0]
                if isinstance(first_resource, dict):
                    key_fields['resource_id'] = first_resource.get('InstanceId') or first_resource.get('id')
        
        # 指标名称
        if 'MetricName' in data:
            key_fields['metric_name'] = data.get('MetricName')
        
        # 告警级别
        if 'Level' in data:
            key_fields['level'] = data.get('Level')
        
        # 通用字段
        if 'alert_id' in data:
            key_fields['alert_id'] = data.get('alert_id')
        if 'alert_name' in data:
            key_fields['alert_name'] = data.get('alert_name')
        if 'resource_id' in data:
            key_fields['resource_id'] = data.get('resource_id')
        if 'service' in data:
            key_fields['service'] = data.get('service')
    
    # 生成稳定的JSON字符串（排序键确保一致性）
    key_string = json.dumps(key_fields, sort_keys=True, ensure_ascii=False)
    
    # 计算SHA256哈希
    hash_value = hashlib.sha256(key_string.encode('utf-8')).hexdigest()
    
    logger.debug(f"生成告警哈希: {hash_value}, 关键字段: {key_fields}")
    return hash_value


def check_duplicate_alert(alert_hash, time_window_hours=None):
    """
    检查是否存在重复告警
    
    Args:
        alert_hash: 告警哈希值
        time_window_hours: 时间窗口（小时），在此时间内的相同告警视为重复
                          如果为None，使用配置文件中的值
    
    Returns:
        tuple: (is_duplicate, original_event)
            is_duplicate: 是否为重复告警
            original_event: 如果是重复告警，返回原始告警事件对象；否则返回None
    """
    if not alert_hash:
        return False, None
    
    # 使用配置文件中的时间窗口设置
    if time_window_hours is None:
        time_window_hours = Config.DUPLICATE_ALERT_TIME_WINDOW
    
    session = get_session()
    try:
        # 计算时间窗口的起始时间
        time_threshold = datetime.now() - timedelta(hours=time_window_hours)
        
        # 查询相同哈希值的告警（时间窗口内，且不是重复告警）
        original_event = session.query(WebhookEvent)\
            .filter(
                WebhookEvent.alert_hash == alert_hash,
                WebhookEvent.timestamp >= time_threshold,
                WebhookEvent.is_duplicate == 0  # 只查找原始告警
            )\
            .order_by(WebhookEvent.timestamp.desc())\
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
        session.close()


def save_webhook_data(data, source='unknown', raw_payload=None, headers=None, client_ip=None, ai_analysis=None, forward_status='pending'):
    """
    保存 webhook 数据到数据库
    
    Args:
        data: webhook 数据(解析后的)
        source: 数据来源
        raw_payload: 原始请求体(bytes)
        headers: 请求头字典
        client_ip: 客户端IP地址
        ai_analysis: AI分析结果
        forward_status: 转发状态
    
    Returns:
        tuple: (webhook_id, is_duplicate, original_event_id)
            webhook_id: 保存的记录 ID
            is_duplicate: 是否为重复告警
            original_event_id: 如果是重复告警，返回原始告警ID
    """
    session = get_session()
    try:
        # 生成告警哈希值
        alert_hash = generate_alert_hash(data, source)
        
        # 检查是否存在重复告警
        is_duplicate, original_event = check_duplicate_alert(alert_hash)
        
        if is_duplicate and original_event:
            # 重复告警：更新原始告警的重复计数，并创建新记录标记为重复
            original_event.duplicate_count = (original_event.duplicate_count or 1) + 1
            original_event.updated_at = datetime.now()
            
            logger.info(f"发现重复告警，原始告警ID={original_event.id}, 已重复{original_event.duplicate_count}次")
            
            # 创建重复告警记录（复用原始AI分析结果）
            webhook_event = WebhookEvent(
                source=source,
                client_ip=client_ip,
                timestamp=datetime.now(),
                raw_payload=raw_payload.decode('utf-8') if raw_payload else None,
                headers=dict(headers) if headers else {},
                parsed_data=data,
                alert_hash=alert_hash,
                ai_analysis=original_event.ai_analysis,  # 复用原始AI分析结果
                importance=original_event.importance,
                forward_status=forward_status,
                is_duplicate=1,
                duplicate_of=original_event.id,
                duplicate_count=1
            )
            
            session.add(webhook_event)
            session.commit()
            
            webhook_id = webhook_event.id
            logger.info(f"重复告警已保存: ID={webhook_id}, 复用原始告警{original_event.id}的AI分析结果")
            
            # 同时保存到文件(保留兼容性)
            save_webhook_to_file(data, source, raw_payload, headers, client_ip, original_event.ai_analysis)
            
            return webhook_id, True, original_event.id
        else:
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
            session.commit()
            
            webhook_id = webhook_event.id
            logger.info(f"Webhook 数据已保存到数据库: ID={webhook_id}")
            
            # 同时保存到文件(保留兼容性)
            save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)
            
            return webhook_id, False, None
        
    except Exception as e:
        session.rollback()
        logger.error(f"保存 webhook 数据到数据库失败: {str(e)}")
        # 失败时至少保存到文件
        file_id = save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)
        return file_id, False, None
    finally:
        session.close()


def save_webhook_to_file(data, source='unknown', raw_payload=None, headers=None, client_ip=None, ai_analysis=None):
    """
    保存 webhook 数据到文件(备份方式)
    
    Args:
        data: webhook 数据(解析后的)
        source: 数据来源
        raw_payload: 原始请求体(bytes)
        headers: 请求头字典
        client_ip: 客户端IP地址
        ai_analysis: AI分析结果
    
    Returns:
        str: 保存的文件路径
    """
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


def get_client_ip(request):
    """
    获取客户端 IP 地址
    
    Args:
        request: Flask request 对象
    
    Returns:
        str: 客户端 IP 地址
    """
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    elif request.headers.get('X-Real-IP'):
        return request.headers.get('X-Real-IP')
    else:
        return request.remote_addr


def get_all_webhooks(page=1, page_size=20):
    """
    从数据库获取所有保存的 webhook 数据（支持分页）
    
    Args:
        page: 页码（从1开始）
        page_size: 每页数量
    
    Returns:
        tuple: (webhook数据列表, 总数量)
    """
    session = get_session()
    try:
        # 查询总数
        total = session.query(WebhookEvent).count()
        
        # 计算偏移量
        offset = (page - 1) * page_size
        
        # 从数据库查询
        events = session.query(WebhookEvent)\
            .order_by(WebhookEvent.timestamp.desc())\
            .limit(page_size)\
            .offset(offset)\
            .all()
        
        # 转换为字典列表
        webhooks = [event.to_dict() for event in events]
        return webhooks, total
        
    except Exception as e:
        logger.error(f"从数据库查询 webhook 数据失败: {str(e)}")
        # 失败时降级为文件查询
        webhooks = get_webhooks_from_files(limit=page_size)
        return webhooks, len(webhooks)
    finally:
        session.close()


def get_webhooks_from_files(limit=50):
    """
    从文件获取 webhook 数据(备份方式)
    
    Args:
        limit: 返回的最大数量
    
    Returns:
        list: webhook 数据列表（按时间倒序）
    """
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
