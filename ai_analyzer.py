import requests
import json
from logger import logger
from config import Config
from openai import OpenAI


def analyze_webhook_with_ai(webhook_data):
    """
    使用 AI 分析 webhook 数据
    
    Args:
        webhook_data: webhook 数据字典
    
    Returns:
        dict: AI 分析结果
    """
    # 检查是否启用 AI 分析
    if not Config.ENABLE_AI_ANALYSIS:
        logger.info("AI 分析功能已禁用，使用基础规则分析")
        source = webhook_data.get('source', 'unknown')
        parsed_data = webhook_data.get('parsed_data', {})
        return analyze_with_rules(parsed_data, source)
    
    # 检查 API Key
    if not Config.OPENAI_API_KEY:
        logger.warning("OpenAI API Key 未配置，降级为规则分析")
        source = webhook_data.get('source', 'unknown')
        parsed_data = webhook_data.get('parsed_data', {})
        return analyze_with_rules(parsed_data, source)
    
    try:
        # 提取关键信息
        source = webhook_data.get('source', 'unknown')
        parsed_data = webhook_data.get('parsed_data', {})
        
        # 使用真实的 OpenAI API 分析
        analysis = analyze_with_openai(parsed_data, source)
        
        logger.info(f"AI 分析完成: {source}")
        return analysis
        
    except Exception as e:
        logger.error(f"AI 分析失败: {str(e)}，降级为规则分析", exc_info=True)
        # 如果 AI 分析失败，降级为规则分析
        source = webhook_data.get('source', 'unknown')
        parsed_data = webhook_data.get('parsed_data', {})
        return analyze_with_rules(parsed_data, source)


def analyze_with_openai(data, source):
    """
    使用 OpenAI API 分析 webhook 数据
    
    Args:
        data: 要分析的数据
        source: 数据来源
    
    Returns:
        dict: AI 分析结果
    """
    try:
        # 初始化 OpenAI 客户端
        client = OpenAI(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_API_URL
        )
        
        # 构建分析提示词
        user_prompt = f"""请分析以下 webhook 事件：

**来源**: {source}
**数据内容**: 
```json
{json.dumps(data, ensure_ascii=False, indent=2)}
```

请按照以下 JSON 格式返回分析结果：

```json
{{
  "source": "来源系统",
  "event_type": "事件类型",
  "importance": "high/medium/low",
  "summary": "事件摘要（中文，50字内）",
  "actions": ["建议操作1", "建议操作2"],
  "risks": ["潜在风险1", "潜在风险2"],
  "impact_scope": "影响范围评估",
  "monitoring_suggestions": ["监控建议1", "监控建议2"]
}}
```

**重要性判断标准**:
- high: 
  * 告警级别为 critical/error/严重/P0
  * 4xx/5xx 状态码 QPS 大幅超过阈值（超过4倍）
  * 服务不可用/故障/错误
  * 安全事件/攻击检测
  * 资金/支付相关异常
  * 数据库相关的异常
  * 对于 CPU 内存 磁盘空间 使用率超过 90% 的
  
- medium: 
  * 告警级别为 warning/警告
  * 4xx/5xx 状态码 QPS 略微超过阈值（2-4倍）
  * 性能问题/慢查询
  * 一般业务警告
  
- low: 
  * 告警级别为 info/information
  * 成功事件/正常操作
  * 常规通知

**特殊识别规则**:
- 如果是云监控告警（包含 Type、RuleName、Level 等字段），重点关注：
  * Level 字段（warning/critical/error/严重/P0）
  * 4xxQPS/5xxQPS 等状态码指标
  * CurrentValue 与 Threshold 的对比
  * Resources 中受影响的资源信息

请直接返回 JSON，不要包含其他文本。"""
        
        # 调用 OpenAI API
        logger.info(f"调用 OpenAI API 分析 webhook: {source}")
        response = client.chat.completions.create(
            model=Config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": Config.AI_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=1000
        )
        
        # 解析响应
        ai_response = response.choices[0].message.content
        if ai_response is None:
            raise ValueError("AI 返回空响应")
        ai_response = ai_response.strip()
        logger.debug(f"AI 响应: {ai_response}")
        
        # 提取 JSON
        if '```json' in ai_response:
            json_start = ai_response.find('```json') + 7
            json_end = ai_response.find('```', json_start)
            ai_response = ai_response[json_start:json_end].strip()
        elif '```' in ai_response:
            json_start = ai_response.find('```') + 3
            json_end = ai_response.find('```', json_start)
            ai_response = ai_response[json_start:json_end].strip()
        
        analysis_result = json.loads(ai_response)
        
        # 确保必需字段存在
        if 'source' not in analysis_result:
            analysis_result['source'] = source
        if 'importance' not in analysis_result:
            analysis_result['importance'] = 'medium'
        
        return analysis_result
        
    except json.JSONDecodeError as e:
        logger.error(f"AI 响应 JSON 解析失败: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"OpenAI API 调用失败: {str(e)}")
        raise


def analyze_with_rules(data, source):
    """
    基于规则的简单分析(可替换为真实 AI)
    
    Args:
        data: 要分析的数据
        source: 数据来源
    
    Returns:
        dict: 分析结果
    """
    # 基础分析结果
    analysis = {
        'source': source,
        'event_type': data.get('event', 'unknown'),
        'importance': 'medium',
        'summary': '',
        'actions': [],
        'risks': []
    }
    
    # 根据事件类型判断重要性
    event = str(data.get('event', '')).lower()
    
    if any(keyword in event for keyword in ['error', 'failure', 'critical', 'alert']):
        analysis['importance'] = 'high'
        analysis['summary'] = f'检测到严重事件: {event}'
        analysis['actions'].append('立即查看详细日志')
        analysis['actions'].append('通知相关负责人')
        analysis['risks'].append('可能影响服务稳定性')
        
    elif any(keyword in event for keyword in ['success', 'completed', 'finished']):
        analysis['importance'] = 'low'
        analysis['summary'] = f'正常完成事件: {event}'
        analysis['actions'].append('记录到日志')
        
    elif any(keyword in event for keyword in ['user', 'order', 'payment']):
        analysis['importance'] = 'high'
        analysis['summary'] = f'业务关键事件: {event}'
        analysis['actions'].append('验证数据完整性')
        analysis['actions'].append('更新业务状态')
        
    else:
        analysis['summary'] = f'一般事件: {event}'
        analysis['actions'].append('常规处理')
    
    # 检查数据字段
    if 'user_id' in data or 'email' in data:
        analysis['data_type'] = 'user_related'
    if 'amount' in data or 'price' in data:
        analysis['data_type'] = 'financial'
        analysis['risks'].append('涉及财务数据,需要额外验证')
    
    # 生成摘要
    if not analysis['summary']:
        analysis['summary'] = f'收到来自 {source} 的 webhook 事件'
    
    return analysis


def forward_to_remote(webhook_data, analysis_result, target_url=None):
    """
    将分析后的数据转发到远程服务器
    
    Args:
        webhook_data: 原始 webhook 数据
        analysis_result: AI 分析结果
        target_url: 目标服务器地址
    
    Returns:
        dict: 转发结果
    """
    # 检查是否启用转发
    if not Config.ENABLE_FORWARD:
        logger.info("转发功能已禁用")
        return {
            'status': 'disabled',
            'message': '转发功能已禁用'
        }
    
    if target_url is None:
        target_url = Config.FORWARD_URL
    
    try:
        # 检查是否是飞书 webhook
        is_feishu = 'feishu.cn' in target_url or 'lark' in target_url
        
        if is_feishu:
            # 构建飞书消息格式
            forward_data = build_feishu_message(webhook_data, analysis_result)
        else:
            # 构建普通转发数据
            forward_data = {
                'original_data': webhook_data.get('parsed_data', {}),
                'original_source': webhook_data.get('source', 'unknown'),
                'original_timestamp': webhook_data.get('timestamp'),
                'ai_analysis': analysis_result,
                'processed_by': 'webhook-analyzer',
                'client_ip': webhook_data.get('client_ip')
            }
        
        # 发送到远程服务器
        headers = {
            'Content-Type': 'application/json'
        }
        
        if not is_feishu:
            headers['X-Webhook-Source'] = f"analyzed-{webhook_data.get('source', 'unknown')}"
            headers['X-Analysis-Importance'] = analysis_result.get('importance', 'unknown')
        
        logger.info(f"转发数据到 {target_url}")
        response = requests.post(
            target_url,
            json=forward_data,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info(f"成功转发到远程服务器: {target_url}")
            return {
                'status': 'success',
                'response': response.json() if response.content else {},
                'status_code': response.status_code
            }
        else:
            logger.warning(f"转发失败,状态码: {response.status_code}")
            return {
                'status': 'failed',
                'status_code': response.status_code,
                'response': response.text
            }
            
    except requests.exceptions.Timeout:
        logger.error(f"转发超时: {target_url}")
        return {
            'status': 'timeout',
            'message': '请求超时'
        }
    except requests.exceptions.ConnectionError:
        logger.error(f"无法连接到远程服务器: {target_url}")
        return {
            'status': 'connection_error',
            'message': '无法连接到远程服务器'
        }
    except Exception as e:
        logger.error(f"转发失败: {str(e)}", exc_info=True)
        return {
            'status': 'error',
            'message': str(e)
        }


def build_feishu_message(webhook_data, analysis_result):
    """
    构建飞书机器人消息格式
    
    Args:
        webhook_data: 原始 webhook 数据
        analysis_result: AI 分析结果
    
    Returns:
        dict: 飞书消息格式
    """
    # 获取基本信息
    source = webhook_data.get('source', 'unknown')
    timestamp = webhook_data.get('timestamp', '')
    importance = analysis_result.get('importance', 'medium')
    summary = analysis_result.get('summary', '无摘要')
    event_type = analysis_result.get('event_type', '未知事件')
    
    # 重要性颜色和 emoji
    importance_map = {
        'high': {'color': 'red', 'emoji': '🔴', 'text': '高'},
        'medium': {'color': 'orange', 'emoji': '🟠', 'text': '中'},
        'low': {'color': 'green', 'emoji': '🟢', 'text': '低'}
    }
    imp_info = importance_map.get(importance, importance_map['medium'])
    
    # 构建卡片消息
    card_content = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📡 Webhook 事件通知"
            },
            "template": imp_info['color']
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**来源**\n{source}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**重要性**\n{imp_info['emoji']} {imp_info['text']}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**事件类型**\n{event_type}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**时间**\n{timestamp[:19] if timestamp else '-'}"
                        }
                    }
                ]
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📝 事件摘要**\n{summary}"
                }
            }
        ]
    }
    
    # 添加影响范围
    if analysis_result.get('impact_scope'):
        card_content['elements'].append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**🎯 影响范围**\n{analysis_result.get('impact_scope')}"
            }
        })
    
    # 添加建议操作
    if analysis_result.get('actions'):
        actions_text = '\n'.join([f"{i+1}. {action}" for i, action in enumerate(analysis_result.get('actions', []))])
        card_content['elements'].append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**✅ 建议操作**\n{actions_text}"
            }
        })
    
    # 添加潜在风险
    if analysis_result.get('risks'):
        risks_text = '\n'.join([f"⚠️ {risk}" for risk in analysis_result.get('risks', [])])
        card_content['elements'].append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**⚠️ 潜在风险**\n{risks_text}"
            }
        })
    
    # 添加分割线
    card_content['elements'].append({
        "tag": "hr"
    })
    
    # 添加原始数据（折叠显示）
    parsed_data = webhook_data.get('parsed_data', {})
    if parsed_data:
        import json
        data_preview = json.dumps(parsed_data, ensure_ascii=False, indent=2)[:500]
        card_content['elements'].append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📦 原始数据**\n```json\n{data_preview}\n```"
            }
        })
    
    return {
        "msg_type": "interactive",
        "card": card_content
    }
