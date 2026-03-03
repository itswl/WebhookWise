import os
import time
import socket
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template, Response
from flask_compress import Compress
from datetime import datetime, timedelta
from dotenv import set_key
from typing import Optional, Generator
from sqlalchemy.exc import IntegrityError

from config import Config
from logger import logger
from utils import (
    verify_signature, save_webhook_data, get_client_ip,
    get_all_webhooks, generate_alert_hash, check_duplicate_alert
)
from ai_analyzer import analyze_webhook_with_ai, forward_to_remote
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
_LOCK_TTL_SECONDS = 120  # 锁过期时间（秒），防止崩溃后死锁
_LOCK_WAIT_SECONDS = 3   # 等待锁的时间（秒）
_LOCK_RETRY_TIMES = 2    # 重试次数


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
        logger.error(f"获取处理锁失败: {e}")
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


def handle_webhook_process(source: Optional[str] = None) -> tuple[Response, int]:
    """通用 Webhook 处理逻辑"""
    try:
        # 获取请求信息
        client_ip = get_client_ip(request)
        signature = request.headers.get('X-Webhook-Signature', '')
        
        # 如果未在路由中指定 source，尝试从 Header 获取
        if source is None:
            source = request.headers.get('X-Webhook-Source', 'unknown')
        
        # 获取原始请求体
        payload = request.get_data()
        
        # 记录接收到的 webhook
        logger.info(f"收到来自 {client_ip} 的 webhook 请求, 来源: {source}")
        logger.debug(f"原始请求体: {payload.decode('utf-8', errors='ignore')[:500]}...")
        logger.debug(f"请求头: {dict(request.headers)}")
        
        # 验证签名
        if signature and not verify_signature(payload, signature):
            logger.warning(f"签名验证失败: IP={client_ip}, Source={source}")
            return jsonify({'success': False, 'error': 'Invalid signature'}), 401
        
        # 解析 JSON 数据
        try:
            data = request.get_json()
        except Exception as e:
            logger.error(f"JSON 解析失败: {str(e)}")
            return jsonify({'success': False, 'error': 'Invalid JSON payload'}), 400
        
        # Webhook 完整数据
        webhook_full_data = {
            'source': source,
            'parsed_data': data,
            'timestamp': datetime.now().isoformat(),
            'client_ip': client_ip
        }
        
        # 去重检测
        alert_hash = generate_alert_hash(data, source)
        
        # 使用数据库分布式锁防止多 worker 并发处理
        with processing_lock(alert_hash) as got_lock:
            if not got_lock:
                # 已有其他 worker 在处理，等待后重新检测
                logger.info(f"等待其他 worker 处理完成: hash={alert_hash[:16]}...")
                time.sleep(_LOCK_WAIT_SECONDS)
                is_duplicate, original_event, beyond_window, last_beyond_window_event = check_duplicate_alert(alert_hash, check_beyond_window=True)
                reanalyzed = False  # 标记是否重新分析

                # 检查是否有其他 worker 刚处理完窗口外重复（避免并发重复分析）
                if last_beyond_window_event and last_beyond_window_event.created_at:
                    seconds_since_created = (datetime.now() - last_beyond_window_event.created_at).total_seconds()
                    if seconds_since_created < 30:  # 30秒内刚创建的记录
                        logger.info(f"检测到其他 worker 刚处理完窗口外重复(ID={last_beyond_window_event.id}, {seconds_since_created:.1f}秒前)，复用结果")
                        analysis_result = last_beyond_window_event.ai_analysis or {}
                        reanalyzed = False
                        # 强制判定为窗口内重复，避免重复转发
                        beyond_window = False
                        is_duplicate = True
                # 优先判断窗口外，因为窗口外也属于重复告警，但需要不同的处理逻辑
                elif beyond_window and original_event:
                    # 窗口外的历史告警
                    if Config.REANALYZE_AFTER_TIME_WINDOW:
                        logger.info(f"窗口外历史告警，重新分析: 历史 ID={original_event.id}")
                        analysis_result = analyze_webhook_with_ai(webhook_full_data)
                        reanalyzed = True
                    else:
                        logger.info(f"窗口外历史告警，复用分析: 历史 ID={original_event.id}")
                        analysis_result = original_event.ai_analysis or {}
                        reanalyzed = False
                elif is_duplicate and original_event:
                    # 其他 worker 已处理完，复用结果（优先复用最近的窗口外记录）
                    if last_beyond_window_event and last_beyond_window_event.ai_analysis:
                        logger.info(f"复用其他 worker 的分析结果: 最近窗口外记录 ID={last_beyond_window_event.id}")
                        analysis_result = last_beyond_window_event.ai_analysis
                    else:
                        logger.info(f"复用其他 worker 的分析结果: 原始 ID={original_event.id}")
                        analysis_result = original_event.ai_analysis or {}
                    reanalyzed = False
                else:
                    # 其他 worker 可能失败了，我们继续处理
                    logger.info("未找到已处理结果，重新处理...")
                    analysis_result = analyze_webhook_with_ai(webhook_full_data)
                    reanalyzed = True
            else:
                # 成功获取锁，正常处理
                is_duplicate, original_event, beyond_window, last_beyond_window_event = check_duplicate_alert(alert_hash, check_beyond_window=True)
                reanalyzed = False  # 标记是否重新分析

                # 优先判断窗口外，因为窗口外也属于重复告警，但需要不同的处理逻辑
                if beyond_window and original_event:
                    # 窗口外的历史告警
                    if Config.REANALYZE_AFTER_TIME_WINDOW:
                        logger.info(f"窗口外历史告警(ID={original_event.id})，重新分析")
                        analysis_result = analyze_webhook_with_ai(webhook_full_data)
                        reanalyzed = True
                    else:
                        logger.info(f"窗口外历史告警(ID={original_event.id})，复用历史分析结果")
                        analysis_result = original_event.ai_analysis or {}
                        reanalyzed = False
                elif is_duplicate and original_event:
                    # 窗口内重复：优先复用最近的窗口外重复记录的分析结果（如果有重新分析的话）
                    if last_beyond_window_event and last_beyond_window_event.ai_analysis:
                        logger.info(f"检测到窗口内重复告警，复用最近窗口外记录 ID={last_beyond_window_event.id} 的分析结果")
                        analysis_result = last_beyond_window_event.ai_analysis
                    else:
                        logger.info(f"检测到窗口内重复告警，复用原始告警 ID={original_event.id} 的分析结果")
                        analysis_result = original_event.ai_analysis or {}
                    reanalyzed = False
                else:
                    logger.info("新告警，开始 AI 分析...")
                    analysis_result = analyze_webhook_with_ai(webhook_full_data)
                    reanalyzed = True

            # 保存数据（传递预先计算的哈希和检测结果，避免重复查询）
            # 注意：窗口外的历史告警也应该标记为重复，只是AI分析可能是新的
            actual_is_duplicate = is_duplicate or beyond_window
            webhook_id, is_dup, original_id, final_beyond_window = save_webhook_data(
                data=data,
                source=source,
                raw_payload=payload,
                headers=request.headers,
                client_ip=client_ip,
                ai_analysis=analysis_result,
                forward_status='pending',
                alert_hash=alert_hash,
                is_duplicate=actual_is_duplicate,
                original_event=original_event,
                beyond_window=beyond_window,  # 传递窗口外标记
                reanalyzed=reanalyzed  # 传递是否重新分析的标记
            )

        # 转发逻辑判断
        # 注意：使用保存后返回的最终状态（可能在重试过程中被重新检测）
        # final_beyond_window 是最终确定的窗口状态
        # is_dup 表示是否为重复告警（窗口内外都是 True）
        beyond_window = final_beyond_window  # 更新为最终状态
        is_duplicate = is_dup and not beyond_window  # 窗口内重复
        importance = analysis_result.get('importance', '').lower()
        should_forward = False
        skip_reason = None
        is_periodic_reminder = False  # 是否为周期性提醒

        if importance == 'high':
            # 注意：先判断窗口外，因为窗口外告警也有 is_duplicate=True
            if beyond_window and not Config.FORWARD_AFTER_TIME_WINDOW:
                # 窗口外的历史重复告警（超过24小时）
                skip_reason = f'窗口外重复告警（原始 ID={original_id}），配置跳过转发'
            elif is_duplicate and not beyond_window:
                # 窗口内的重复告警
                # 检查是否需要周期性提醒
                if Config.ENABLE_PERIODIC_REMINDER and original_event:
                    from datetime import timedelta

                    # 计算距离上次通知的时间
                    last_notified = original_event.last_notified_at
                    if last_notified:
                        time_since_notification = (datetime.now() - last_notified).total_seconds() / 3600
                        if time_since_notification >= Config.REMINDER_INTERVAL_HOURS:
                            # 需要周期性提醒
                            should_forward = True
                            is_periodic_reminder = True
                            logger.info(f"触发周期性提醒: 原始ID={original_id}, 距上次通知{time_since_notification:.1f}小时, 已重复{original_event.duplicate_count}次")
                        else:
                            # 尚未到提醒时间
                            skip_reason = f'窗口内重复告警（原始 ID={original_id}），距上次通知仅{time_since_notification:.1f}小时'
                    else:
                        # 首次通知后的重复（原始告警有 last_notified_at，但可能为 None）
                        # 理论上新告警创建时会设置，这里做个保护
                        if not Config.FORWARD_DUPLICATE_ALERTS:
                            skip_reason = f'窗口内重复告警（原始 ID={original_id}），配置跳过转发'
                        else:
                            should_forward = True
                else:
                    # 未启用周期性提醒，按原逻辑
                    if not Config.FORWARD_DUPLICATE_ALERTS:
                        skip_reason = f'窗口内重复告警（原始 ID={original_id}），配置跳过转发'
                    else:
                        should_forward = True
            else:
                should_forward = True
        else:
            skip_reason = f'重要性为 {importance}，非高风险事件不自动转发'

        forward_result = {'status': 'skipped', 'reason': skip_reason}
        if should_forward:
            alert_type = '周期性提醒' if is_periodic_reminder else ('窗口内重复' if is_duplicate else ('窗口外重复' if beyond_window else '新'))
            logger.info(f"开始自动转发高风险{alert_type}告警...")
            forward_result = forward_to_remote(webhook_full_data, analysis_result, is_periodic_reminder=is_periodic_reminder)

            # 更新原始告警的 last_notified_at（如果成功转发）
            if forward_result.get('status') == 'success' and original_event:
                try:
                    from models import get_session
                    from sqlalchemy import update
                    with get_session() as session:
                        session.execute(
                            update(WebhookEvent)
                            .where(WebhookEvent.id == original_event.id)
                            .values(last_notified_at=datetime.now())
                        )
                        session.commit()
                        logger.info(f"已更新原始告警 {original_event.id} 的 last_notified_at")
                except Exception as e:
                    logger.warning(f"更新 last_notified_at 失败: {e}")
        else:
            logger.info(f"跳过自动转发: {skip_reason}")
            
        # 检查是否发生了 AI 降级
        is_degraded = analysis_result.get('_degraded', False)
        degraded_reason = analysis_result.get('_degraded_reason')

        # 移除内部标记字段（不返回给客户端）
        clean_analysis = {k: v for k, v in analysis_result.items() if not k.startswith('_')}

        return jsonify({
            'success': True,
            'message': 'Webhook processed successfully',
            'timestamp': datetime.now().isoformat(),
            'webhook_id': webhook_id,
            'ai_analysis': clean_analysis,
            'ai_degraded': is_degraded,  # 是否发生降级
            'ai_degraded_reason': degraded_reason if is_degraded else None,  # 降级原因
            'forward_status': forward_result.get('status', 'unknown'),
            'is_duplicate': is_dup,
            'duplicate_of': original_id if is_dup else None,
            'beyond_time_window': beyond_window,  # 直接返回窗口外标记
            'is_within_window': is_duplicate  # 新增：窗口内重复标记，便于前端区分
        }), 200

    except Exception as e:
        logger.error(f"处理 Webhook 时发生错误: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'service': 'webhook-receiver'
    }), 200


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

    return jsonify({
        'success': True,
        'data': webhooks,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size if total > 0 else 0,
            'next_cursor': next_cursor  # 游标分页支持
        }
    }), 200


@app.route('/api/webhooks/<int:webhook_id>', methods=['GET'])
def get_webhook_detail(webhook_id: int) -> tuple[Response, int]:
    """获取单条 webhook 详细信息（完整数据）"""
    try:
        with session_scope() as session:
            event = session.query(WebhookEvent).filter_by(id=webhook_id).first()
            if not event:
                return jsonify({'success': False, 'error': 'Webhook not found'}), 404

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

            return jsonify({
                'success': True,
                'data': data
            }), 200
    except Exception as e:
        logger.error(f"查询 webhook 详情失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config', methods=['GET'])
def get_config():
    """获取当前配置（从 .env 文件实时读取）"""
    try:
        import os
        from pathlib import Path
        from dotenv import dotenv_values

        # 读取 .env 文件获取最新配置
        env_path = Path('.env')
        env_values = {}

        if env_path.exists():
            env_values = dotenv_values(env_path)

        # 优先使用 .env 文件的值，如果没有则使用 Config 类的值（环境变量或默认值）
        def get_value(key, default=None, value_type='str'):
            # 先从 .env 文件读取
            val = env_values.get(key)
            # 如果 .env 没有，从 Config 类读取
            if val is None:
                val = getattr(Config, key, default)

            # 类型转换
            if value_type == 'bool':
                if isinstance(val, str):
                    return val.lower() == 'true'
                return bool(val)
            elif value_type == 'int':
                return int(val) if val else default
            return val

        # 不返回完整的敏感信息，只返回是否已配置
        api_key = get_value('OPENAI_API_KEY', '')
        masked_key = '已配置' if api_key else '未配置'

        config_data = {
            'forward_url': get_value('FORWARD_URL', ''),
            'enable_forward': get_value('ENABLE_FORWARD', False, 'bool'),
            'enable_ai_analysis': get_value('ENABLE_AI_ANALYSIS', True, 'bool'),
            'openai_api_key': masked_key,  # 脱敏处理
            'openai_api_url': get_value('OPENAI_API_URL', 'https://openrouter.ai/api/v1'),
            'openai_model': get_value('OPENAI_MODEL', 'anthropic/claude-sonnet-4'),
            'ai_system_prompt': get_value('AI_SYSTEM_PROMPT', Config.AI_SYSTEM_PROMPT),
            'log_level': get_value('LOG_LEVEL', 'INFO'),
            'duplicate_alert_time_window': get_value('DUPLICATE_ALERT_TIME_WINDOW', 24, 'int'),
            'forward_duplicate_alerts': get_value('FORWARD_DUPLICATE_ALERTS', False, 'bool'),
            'reanalyze_after_time_window': get_value('REANALYZE_AFTER_TIME_WINDOW', True, 'bool'),
            'forward_after_time_window': get_value('FORWARD_AFTER_TIME_WINDOW', True, 'bool')
        }

        return jsonify({
            'success': True,
            'data': config_data
        }), 200
    except Exception as e:
        logger.error(f"获取配置失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/config', methods=['POST'])
def update_config():
    """更新配置"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': '请求体为空'}), 400

        env_file = '.env'

        # 配置项定义：(env_var, type, validator)
        config_schema = {
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
            'forward_after_time_window': ('FORWARD_AFTER_TIME_WINDOW', 'bool', None)
        }

        # 收集要更新的配置
        updates = {}
        errors = []

        for key, val in data.items():
            if key not in config_schema:
                continue

            env_var, val_type, validator = config_schema[key]

            # 类型验证和转换
            try:
                if val_type == 'bool':
                    if isinstance(val, bool):
                        typed_val = val
                    elif isinstance(val, str):
                        typed_val = val.lower() == 'true'
                    else:
                        raise ValueError(f"{key} 应为布尔类型")
                    updates[env_var] = (str(typed_val).lower(), typed_val)
                elif val_type == 'int':
                    typed_val = int(val)
                    if validator and not validator(typed_val):
                        raise ValueError(f"{key} 值超出有效范围")
                    updates[env_var] = (str(typed_val), typed_val)
                else:  # str
                    typed_val = str(val).strip()
                    # 跳过空字符串（避免覆盖已有配置）
                    if not typed_val:
                        logger.debug(f"跳过空值配置: {key}")
                        continue
                    if validator and not validator(typed_val):
                        raise ValueError(f"{key} 格式无效")
                    updates[env_var] = (typed_val, typed_val)
            except ValueError as e:
                errors.append(str(e))

        if errors:
            return jsonify({'success': False, 'error': '; '.join(errors)}), 400

        # 批量更新 .env 文件
        try:
            import os
            from pathlib import Path

            env_path = Path(env_file)

            # 读取现有内容
            if env_path.exists():
                with open(env_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
            else:
                lines = []

            # 更新或添加配置
            updated_vars = set()
            new_lines = []

            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    new_lines.append(line)
                    continue

                # 解析配置行
                if '=' in stripped:
                    var_name = stripped.split('=', 1)[0].strip()
                    if var_name in updates:
                        new_value, _ = updates[var_name]
                        new_lines.append(f'{var_name}={new_value}\n')
                        updated_vars.add(var_name)
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)

            # 添加新配置
            for var_name, (str_val, _) in updates.items():
                if var_name not in updated_vars:
                    new_lines.append(f'{var_name}={str_val}\n')

            # Docker 挂载文件无法使用 rename/replace，直接覆盖写入
            try:
                # 直接覆盖写入原文件
                with open(env_path, 'w', encoding='utf-8') as f:
                    f.writelines(new_lines)
                    f.flush()  # 确保写入磁盘

                # 更新运行时配置
                for var_name, (_, typed_val) in updates.items():
                    setattr(Config, var_name, typed_val)
                    os.environ[var_name] = str(typed_val) if not isinstance(typed_val, bool) else str(typed_val).lower()

                logger.info(f"配置已更新: {list(updates.keys())}")
                return jsonify({'success': True, 'message': '配置更新成功'}), 200

            except Exception as e:
                logger.error(f"写入配置文件失败: {str(e)}", exc_info=True)
                raise

        except PermissionError as e:
            logger.error(f"权限错误，无法写入 .env 文件: {str(e)}")
            return jsonify({
                'success': False,
                'error': f'权限错误: 无法写入配置文件。请检查 .env 文件权限或使用环境变量配置。'
            }), 500
        except Exception as e:
            logger.error(f"更新 .env 文件失败: {str(e)}", exc_info=True)
            raise

    except Exception as e:
        logger.error(f"更新配置失败: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/reanalyze/<int:webhook_id>', methods=['POST'])
def reanalyze_webhook(webhook_id: int) -> tuple[Response, int]:
    """重新分析指定的 webhook，并更新所有引用它的重复告警"""
    try:
        with session_scope() as session:
            # 从数据库获取 webhook
            webhook_event = session.query(WebhookEvent).filter_by(id=webhook_id).first()

            if not webhook_event:
                return jsonify({'success': False, 'error': 'Webhook not found'}), 404

            # 准备分析数据
            webhook_data = {
                'source': webhook_event.source,
                'parsed_data': webhook_event.parsed_data,
                'timestamp': webhook_event.timestamp.isoformat() if webhook_event.timestamp else None,
                'client_ip': webhook_event.client_ip
            }

            # 重新进行 AI 分析
            logger.info(f"重新分析 webhook ID: {webhook_id}")
            analysis_result = analyze_webhook_with_ai(webhook_data)

            # 更新原始告警
            old_importance = webhook_event.importance
            webhook_event.ai_analysis = analysis_result
            webhook_event.importance = analysis_result.get('importance')

            new_importance = analysis_result.get('importance')
            logger.info(f"重新分析完成: {old_importance} → {new_importance} - {analysis_result.get('summary', '')}")

            # 如果这是原始告警（is_duplicate=0），更新所有引用它的重复告警
            updated_duplicates = 0
            if webhook_event.is_duplicate == 0:
                # 查找所有引用此告警的重复记录
                duplicate_events = session.query(WebhookEvent)\
                    .filter(WebhookEvent.duplicate_of == webhook_id)\
                    .all()

                if duplicate_events:
                    for dup in duplicate_events:
                        dup.ai_analysis = analysis_result
                        dup.importance = new_importance
                        updated_duplicates += 1

                    logger.info(f"同时更新了 {updated_duplicates} 条重复告警的分析结果")

            return jsonify({
                'success': True,
                'analysis': analysis_result,
                'original_importance': old_importance,
                'new_importance': new_importance,
                'updated_duplicates': updated_duplicates,
                'message': f'重新分析完成，importance: {old_importance} → {new_importance}' +
                          (f'，同时更新了 {updated_duplicates} 条重复告警' if updated_duplicates > 0 else '')
            }), 200

    except Exception as e:
        logger.error(f"重新分析失败: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/forward/<int:webhook_id>', methods=['POST'])
def manual_forward_webhook(webhook_id: int) -> tuple[Response, int]:
    """手动转发指定的 webhook"""
    try:
        with session_scope() as session:
            # 从数据库获取 webhook
            webhook_event = session.query(WebhookEvent).filter_by(id=webhook_id).first()
            
            if not webhook_event:
                return jsonify({'success': False, 'error': 'Webhook not found'}), 404
            
            # 准备转发数据
            webhook_data = {
                'source': webhook_event.source,
                'parsed_data': webhook_event.parsed_data,
                'timestamp': webhook_event.timestamp.isoformat() if webhook_event.timestamp else None,
                'client_ip': webhook_event.client_ip
            }
            
            # 获取自定义转发地址（如果提供）
            custom_url = request.json.get('forward_url') if request.json else None
            
            logger.info(f"手动转发 webhook ID: {webhook_id} 到 {custom_url or Config.FORWARD_URL}")
            
            # 转发数据
            analysis_result = webhook_event.ai_analysis or {}
            forward_result = forward_to_remote(webhook_data, analysis_result, custom_url)
            
            # 更新转发状态
            webhook_event.forward_status = forward_result.get('status', 'unknown')
            
            return jsonify({
                'success': forward_result.get('status') == 'success',
                'result': forward_result,
                'message': 'Forward completed'
            }), 200
        
    except Exception as e:
        logger.error(f"手动转发失败: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/prompt/reload', methods=['POST'])
def reload_prompt() -> tuple[Response, int]:
    """重新加载 AI Prompt 模板"""
    try:
        from ai_analyzer import reload_user_prompt_template

        # 重新加载模板
        new_template = reload_user_prompt_template()

        logger.info("AI Prompt 模板已重新加载")

        return jsonify({
            'success': True,
            'message': 'Prompt 模板已重新加载',
            'template_length': len(new_template),
            'preview': new_template[:200] + '...' if len(new_template) > 200 else new_template
        }), 200

    except Exception as e:
        logger.error(f"重新加载 prompt 模板失败: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/prompt', methods=['GET'])
def get_prompt() -> tuple[Response, int]:
    """获取当前 AI Prompt 模板"""
    try:
        from ai_analyzer import load_user_prompt_template

        template = load_user_prompt_template()

        return jsonify({
            'success': True,
            'template': template,
            'source': 'environment' if Config.AI_USER_PROMPT else ('file' if Config.AI_USER_PROMPT_FILE else 'default')
        }), 200

    except Exception as e:
        logger.error(f"获取 prompt 模板失败: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


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
    return jsonify({
        'success': False,
        'error': 'Endpoint not found'
    }), 404


@app.errorhandler(405)
def method_not_allowed(error):
    """405 错误处理"""
    return jsonify({
        'success': False,
        'error': 'Method not allowed'
    }), 405


@app.route('/api/migrations/add_unique_constraint', methods=['POST'])
def migration_add_unique_constraint() -> tuple[Response, int]:
    """执行数据库迁移：添加唯一约束"""
    try:
        # 导入迁移工具
        from migrations_tool import add_unique_constraint

        logger.info("开始执行数据库迁移：添加唯一约束")

        success = add_unique_constraint()

        if success:
            return jsonify({
                'success': True,
                'message': '数据库迁移成功：唯一约束已添加'
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': '数据库迁移失败，请查看日志'
            }), 500

    except Exception as e:
        logger.error(f"执行迁移失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


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
