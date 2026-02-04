import os
from flask import Flask, request, jsonify, render_template
from datetime import datetime
from dotenv import set_key

from config import Config
from logger import logger
from utils import (
    verify_signature, save_webhook_data, get_client_ip, 
    get_all_webhooks, generate_alert_hash, check_duplicate_alert
)
from ai_analyzer import analyze_webhook_with_ai, forward_to_remote
from models import WebhookEvent, get_session, session_scope

app = Flask(__name__)
app.config.from_object(Config)


def handle_webhook_process(source=None):
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
        is_duplicate, original_event = check_duplicate_alert(alert_hash)
        
        if is_duplicate and original_event:
            logger.info(f"检测到重复告警(hash={alert_hash})，复用 ID={original_event.id} 的分析结果")
            analysis_result = original_event.ai_analysis or {}
        else:
            logger.info("新告警，开始 AI 分析...")
            analysis_result = analyze_webhook_with_ai(webhook_full_data)
        
        # 保存数据（传递预先计算的哈希和检测结果，避免重复查询）
        webhook_id, is_dup, original_id = save_webhook_data(
            data=data, 
            source=source,
            raw_payload=payload,
            headers=request.headers,
            client_ip=client_ip,
            ai_analysis=analysis_result,
            forward_status='pending',
            alert_hash=alert_hash,
            is_duplicate=is_duplicate,
            original_event=original_event
        )
        
        # 转发逻辑判断
        importance = analysis_result.get('importance', '').lower()
        should_forward = False
        skip_reason = None
        
        if importance == 'high':
            if is_dup and not Config.FORWARD_DUPLICATE_ALERTS:
                skip_reason = f'重复告警（原始 ID={original_id}），配置跳过转发'
            else:
                should_forward = True
        else:
            skip_reason = f'重要性为 {importance}，非高风险事件不自动转发'
        
        forward_result = {'status': 'skipped', 'reason': skip_reason}
        if should_forward:
            logger.info(f"开始自动转发高风险{'重复' if is_dup else ''}告警...")
            forward_result = forward_to_remote(webhook_full_data, analysis_result)
        else:
            logger.info(f"跳过自动转发: {skip_reason}")
            
        return jsonify({
            'success': True,
            'message': 'Webhook processed successfully',
            'timestamp': datetime.now().isoformat(),
            'webhook_id': webhook_id,
            'ai_analysis': analysis_result,
            'forward_status': forward_result.get('status', 'unknown'),
            'is_duplicate': is_dup,
            'duplicate_of': original_id if is_dup else None
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
def list_webhooks():
    """获取 webhook 列表 API（支持分页）"""
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    
    # 限制每页最大数量
    page_size = min(page_size, 100)
    
    webhooks, total = get_all_webhooks(page=page, page_size=page_size)
    
    return jsonify({
        'success': True,
        'data': webhooks,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size if total > 0 else 0
        }
    }), 200


@app.route('/api/config', methods=['GET'])
def get_config():
    """获取当前配置"""
    try:
        # 不返回完整的敏感信息，只返回是否已配置
        api_key = Config.OPENAI_API_KEY
        masked_key = f"{api_key[:8]}...{api_key[-4:]}" if api_key and len(api_key) > 12 else ('已配置' if api_key else '未配置')
        
        config_data = {
            'forward_url': Config.FORWARD_URL,
            'enable_forward': Config.ENABLE_FORWARD,
            'enable_ai_analysis': Config.ENABLE_AI_ANALYSIS,
            'openai_api_key': masked_key,  # 脱敏处理
            'openai_api_url': Config.OPENAI_API_URL,
            'openai_model': Config.OPENAI_MODEL,
            'ai_system_prompt': Config.AI_SYSTEM_PROMPT,
            'log_level': Config.LOG_LEVEL,
            'duplicate_alert_time_window': Config.DUPLICATE_ALERT_TIME_WINDOW,
            'forward_duplicate_alerts': Config.FORWARD_DUPLICATE_ALERTS
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
            'forward_duplicate_alerts': ('FORWARD_DUPLICATE_ALERTS', 'bool', None)
        }
        
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
                    set_key(env_file, env_var, str(typed_val).lower())
                    setattr(Config, env_var, typed_val)
                elif val_type == 'int':
                    typed_val = int(val)
                    if validator and not validator(typed_val):
                        raise ValueError(f"{key} 值超出有效范围")
                    set_key(env_file, env_var, str(typed_val))
                    setattr(Config, env_var, typed_val)
                else:  # str
                    typed_val = str(val)
                    if validator and not validator(typed_val):
                        raise ValueError(f"{key} 格式无效")
                    set_key(env_file, env_var, typed_val)
                    setattr(Config, env_var, typed_val)
            except ValueError as e:
                errors.append(str(e))
        
        if errors:
            return jsonify({'success': False, 'error': '; '.join(errors)}), 400
        
        logger.info("配置已更新")
        return jsonify({'success': True, 'message': '配置更新成功'}), 200
        
    except Exception as e:
        logger.error(f"更新配置失败: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/reanalyze/<int:webhook_id>', methods=['POST'])
def reanalyze_webhook(webhook_id):
    """重新分析指定的 webhook"""
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
            
            # 更新数据库
            webhook_event.ai_analysis = analysis_result
            webhook_event.importance = analysis_result.get('importance')
            
            logger.info(f"重新分析完成: {analysis_result.get('importance', 'unknown')} - {analysis_result.get('summary', '')}")
            
            return jsonify({
                'success': True,
                'analysis': analysis_result,
                'message': 'Reanalysis completed successfully'
            }), 200
        
    except Exception as e:
        logger.error(f"重新分析失败: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/forward/<int:webhook_id>', methods=['POST'])
def manual_forward_webhook(webhook_id):
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


if __name__ == '__main__':
    logger.info(f"启动 Webhook 服务: http://{Config.HOST}:{Config.PORT}")
    app.run(
        host=Config.HOST,
        port=Config.PORT,
        debug=Config.DEBUG
    )
