import requests
import json
import re
import os
from typing import Any, Optional
from pathlib import Path

try:
    import json5
    HAS_JSON5 = True
except ImportError:
    HAS_JSON5 = False

from logger import logger
from config import Config
from openai import OpenAI

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]
ForwardResult = dict[str, Any]

# 缓存 prompt 模板
_user_prompt_template: Optional[str] = None


def load_user_prompt_template() -> str:
    """
    加载 User Prompt 模板

    优先级：
    1. 环境变量 AI_USER_PROMPT（直接内容）
    2. 文件 AI_USER_PROMPT_FILE
    3. 默认硬编码模板

    Returns:
        str: Prompt 模板字符串，支持 {source} 和 {data_json} 占位符
    """
    global _user_prompt_template

    # 如果已缓存，直接返回
    if _user_prompt_template is not None:
        return _user_prompt_template

    # 1. 优先使用环境变量中的直接内容
    if Config.AI_USER_PROMPT:
        logger.info("使用环境变量 AI_USER_PROMPT 中的 prompt 模板")
        _user_prompt_template = Config.AI_USER_PROMPT
        return _user_prompt_template

    # 2. 尝试从文件加载
    prompt_file = Config.AI_USER_PROMPT_FILE
    if prompt_file:
        # 支持相对路径和绝对路径
        file_path = Path(prompt_file)
        if not file_path.is_absolute():
            # 相对于项目根目录
            file_path = Path(__file__).parent / file_path

        if file_path.exists():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    _user_prompt_template = f.read()
                logger.info(f"成功从文件加载 prompt 模板: {file_path}")
                return _user_prompt_template
            except Exception as e:
                logger.warning(f"从文件加载 prompt 模板失败: {e}，使用默认模板")
        else:
            logger.warning(f"Prompt 模板文件不存在: {file_path}，使用默认模板")

    # 3. 使用默认模板
    logger.info("使用默认硬编码 prompt 模板")
    _user_prompt_template = """请分析以下 webhook 事件：

**来源**: {source}
**数据内容**:
```json
{data_json}
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

**重要提示**:
1. 必须返回严格的 JSON 格式
2. 不要在 JSON 中使用注释
3. 数组中最后一个元素后不要有逗号
4. 所有字符串必须用双引号
5. 直接返回 JSON，不要包含其他文本和解释"""

    return _user_prompt_template


def reload_user_prompt_template() -> str:
    """
    重新加载 User Prompt 模板（清除缓存后重新加载）

    用于运行时动态更新 prompt 模板

    Returns:
        str: 新加载的 Prompt 模板字符串
    """
    global _user_prompt_template
    _user_prompt_template = None
    logger.info("清除 prompt 模板缓存，重新加载")
    return load_user_prompt_template()


def fix_json_format(json_str: str) -> str:
    """修复常见的 JSON 格式错误"""
    # 移除 BOM 和特殊字符
    json_str = json_str.replace('\ufeff', '').strip()
    
    # 先尝试直接解析
    try:
        json.loads(json_str)
        return json_str
    except json.JSONDecodeError:
        pass
    
    # 如果有 json5 库，使用它来解析（支持尾随逗号、单引号、注释等）
    if HAS_JSON5:
        try:
            parsed = json5.loads(json_str)
            # 转换回标准 JSON
            return json.dumps(parsed, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"json5 解析失败: {e}")
    
    # 兖底: 简单的正则修复
    try:
        # 移除注释
        json_str = re.sub(r'//.*?$', '', json_str, flags=re.MULTILINE)
        json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
        # 修复尾随逗号
        json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
        # 修复起始逗号
        json_str = re.sub(r'([{\[])\s*,', r'\1', json_str)
        
        json.loads(json_str)
        logger.debug("JSON 格式修复成功")
    except json.JSONDecodeError as e:
        logger.warning(f"JSON 格式修复后仍然无效: {e}")
    
    return json_str.strip()


def extract_from_text(text: str, source: str) -> AnalysisResult:
    """从 AI 响应文本中提取关键信息（兖底策略）"""
    logger.info("使用文本提取策略解析 AI 响应")
    
    result = {
        'source': source,
        'event_type': 'unknown',
        'importance': 'medium',
        'summary': '',
        'actions': [],
        'risks': []
    }
    
    try:
        # 提取重要性
        if re.search(r'importance["\s:]+high', text, re.IGNORECASE):
            result['importance'] = 'high'
        elif re.search(r'importance["\s:]+low', text, re.IGNORECASE):
            result['importance'] = 'low'
        elif re.search(r'(高|critical|严重)', text):
            result['importance'] = 'high'
        elif re.search(r'(低|info|正常)', text):
            result['importance'] = 'low'
        
        # 提取摘要
        summary_match = re.search(r'summary["\s:]+["\']([^"\']+)["\']', text, re.IGNORECASE)
        if summary_match:
            result['summary'] = summary_match.group(1)
        elif re.search(r'(告警|错误|异常|故障)', text):
            result['summary'] = '检测到系统告警或异常，需要关注'
        else:
            result['summary'] = 'Webhook 事件已接收，AI 分析结果解析不完整'
        
        # 提取事件类型
        event_match = re.search(r'event_type["\s:]+["\']([^"\']+)["\']', text, re.IGNORECASE)
        if event_match:
            result['event_type'] = event_match.group(1)
        
        # 提取建议操作
        actions_match = re.findall(r'(?:操作|action)[^:]*[:：]\s*["\']?([^"\'}\],]+)', text, re.IGNORECASE)
        if actions_match:
            result['actions'] = [a.strip() for a in actions_match if a.strip()]
        
        # 提取风险
        risks_match = re.findall(r'(?:风险|risk)[^:]*[:：]\s*["\']?([^"\'}\],]+)', text, re.IGNORECASE)
        if risks_match:
            result['risks'] = [r.strip() for r in risks_match if r.strip()]
        
        # 提取影响范围
        impact_match = re.search(r'impact_scope["\s:]+["\']([^"\']+)["\']', text, re.IGNORECASE)
        if impact_match:
            result['impact_scope'] = impact_match.group(1)
        
        logger.info(f"文本提取完成: {result}")
        return result
        
    except Exception as e:
        logger.error(f"文本提取失败: {str(e)}")
        result['summary'] = 'AI 分析响应格式错误，已降级处理'
        return result


def analyze_webhook_with_ai(webhook_data: WebhookData) -> AnalysisResult:
    """使用 AI 分析 webhook 数据"""
    source = webhook_data.get('source', 'unknown')
    parsed_data = webhook_data.get('parsed_data', {})

    # 检查是否启用 AI 分析
    if not Config.ENABLE_AI_ANALYSIS:
        logger.info("AI 分析功能已禁用，使用基础规则分析")
        result = analyze_with_rules(parsed_data, source)
        result['_degraded'] = True
        result['_degraded_reason'] = 'AI 分析功能已禁用'
        return result

    # 检查 API Key
    if not Config.OPENAI_API_KEY:
        logger.warning("OpenAI API Key 未配置，降级为规则分析")
        result = analyze_with_rules(parsed_data, source)
        result['_degraded'] = True
        result['_degraded_reason'] = 'OpenAI API Key 未配置'
        # 发送降级通知
        _send_degradation_alert(webhook_data, 'OpenAI API Key 未配置')
        return result

    try:
        # 使用真实的 OpenAI API 分析
        analysis = analyze_with_openai(parsed_data, source)

        logger.info(f"AI 分析完成: {source}")
        analysis['_degraded'] = False
        return analysis

    except Exception as e:
        logger.error(f"AI 分析失败: {str(e)}，降级为规则分析", exc_info=True)
        # 如果 AI 分析失败，降级为规则分析
        result = analyze_with_rules(parsed_data, source)
        result['_degraded'] = True
        result['_degraded_reason'] = f'AI 分析失败: {str(e)}'
        # 发送降级通知
        _send_degradation_alert(webhook_data, str(e))
        return result


def analyze_with_openai(data: dict[str, Any], source: str) -> AnalysisResult:
    """使用 OpenAI API 分析 webhook 数据"""
    try:
        # 初始化 OpenAI 客户端
        client = OpenAI(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_API_URL
        )

        # 加载 prompt 模板并格式化
        prompt_template = load_user_prompt_template()
        data_json = json.dumps(data, ensure_ascii=False, indent=2)

        # 使用模板格式化用户输入
        user_prompt = prompt_template.format(
            source=source,
            data_json=data_json
        )

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
        
        # 增加对响应类型的检查，防止API返回非JSON错误
        if not hasattr(response, 'choices') or not response.choices:
            error_message = f"OpenAI API 返回无效响应: {response}"
            logger.error(error_message)
            raise TypeError(error_message)

        # 解析响应
        ai_response = response.choices[0].message.content
        if ai_response is None:
            raise ValueError("AI 返回空响应")
        ai_response = ai_response.strip()
        logger.debug(f"AI 原始响应: {ai_response}")
        
        # 提取 JSON
        if '```json' in ai_response:
            json_start = ai_response.find('```json') + 7
            json_end = ai_response.find('```', json_start)
            ai_response = ai_response[json_start:json_end].strip()
        elif '```' in ai_response:
            json_start = ai_response.find('```') + 3
            json_end = ai_response.find('```', json_start)
            ai_response = ai_response[json_start:json_end].strip()
        
        logger.debug(f"提取的 JSON: {ai_response}")
        
        # 尝试修复常见的 JSON 格式错误
        ai_response = fix_json_format(ai_response)
        
        try:
            analysis_result = json.loads(ai_response)
        except json.JSONDecodeError as e:
            # 记录详细的错误信息
            logger.error(f"JSON 解析失败: {str(e)}")
            logger.error(f"问题位置: 第 {e.lineno} 行, 第 {e.colno} 列")
            logger.error(f"错误内容: {ai_response}")
            
            # 尝试提取部分有效的 JSON
            # 找到第一个完整的对象
            brace_count = 0
            valid_end = -1
            for i, char in enumerate(ai_response):
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        valid_end = i + 1
                        break
            
            if valid_end > 0:
                logger.info(f"尝试提取前 {valid_end} 个字符作为有效 JSON")
                try:
                    analysis_result = json.loads(ai_response[:valid_end])
                    logger.info("成功提取部分 JSON")
                except:
                    # 如果仍然失败，尝试从文本中提取关键信息
                    logger.warning("JSON 提取失败，尝试从文本中解析关键信息")
                    analysis_result = extract_from_text(ai_response, source)
            else:
                # 如果找不到完整对象，尝试从文本中提取关键信息
                logger.warning("未找到完整的 JSON 对象，尝试从文本中解析关键信息")
                analysis_result = extract_from_text(ai_response, source)
        
        # 确保必需字段存在
        if 'source' not in analysis_result:
            analysis_result['source'] = source
        if 'importance' not in analysis_result:
            analysis_result['importance'] = 'medium'
        if 'summary' not in analysis_result or not analysis_result['summary']:
            analysis_result['summary'] = 'AI分析未生成摘要'
        
        return analysis_result
        
    except json.JSONDecodeError as e:
        logger.error(f"AI 响应 JSON 解析失败: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"OpenAI API 调用失败: {str(e)}")
        raise


def _should_send_degradation_alert() -> bool:
    """
    检查是否应该发送降级通知（24小时限流）

    使用文件记录上次通知时间，避免频繁通知

    Returns:
        bool: True - 应该发送，False - 跳过（24小时内已通知过）
    """
    from datetime import datetime, timedelta
    from pathlib import Path

    # 使用临时文件记录上次通知时间
    marker_file = Path('/tmp/.ai_degradation_last_alert')

    try:
        # 读取上次通知时间
        if marker_file.exists():
            with open(marker_file, 'r') as f:
                last_alert_time_str = f.read().strip()
                last_alert_time = datetime.fromisoformat(last_alert_time_str)

            # 检查是否在24小时内
            time_since_last = datetime.now() - last_alert_time
            if time_since_last < timedelta(hours=24):
                hours_remaining = 24 - (time_since_last.total_seconds() / 3600)
                logger.info(f"跳过降级通知：距离上次通知仅 {time_since_last.total_seconds() / 3600:.1f} 小时，还需等待 {hours_remaining:.1f} 小时")
                return False

        # 记录本次通知时间
        with open(marker_file, 'w') as f:
            f.write(datetime.now().isoformat())

        return True

    except Exception as e:
        logger.error(f"检查降级通知限流失败: {e}，默认允许发送")
        return True


def _send_degradation_alert(webhook_data: WebhookData, error_reason: str) -> None:
    """发送 AI 降级通知（带24小时限流）"""
    try:
        # 检查是否在限流期内
        if not _should_send_degradation_alert():
            return

        # 只有启用转发且配置了转发地址才发送
        if not Config.ENABLE_FORWARD or not Config.FORWARD_URL:
            logger.info("转发未启用，跳过降级通知")
            return

        # 检查是否是飞书 webhook
        is_feishu = 'feishu.cn' in Config.FORWARD_URL or 'lark' in Config.FORWARD_URL

        if is_feishu:
            # 构建飞书告警消息
            timestamp = webhook_data.get('timestamp', '')
            source = webhook_data.get('source', 'unknown')

            card_content = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "⚠️ AI 分析降级通知"
                    },
                    "template": "orange"  # 橙色警告
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**告警来源**: {source}\n**时间**: {timestamp[:19] if timestamp else '-'}"
                        }
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**⚠️ 降级原因**\n{error_reason}"
                        }
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "**处理方式**\n已自动降级为基于规则的分析，告警仍会正常处理，但分析结果可能不够准确。请检查 AI 服务配置。"
                        }
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "💡 此通知24小时内仅发送一次，避免频繁打扰。请尽快修复 AI 服务以恢复智能分析功能。"
                            }
                        ]
                    }
                ]
            }

            forward_data = {
                "msg_type": "interactive",
                "card": card_content
            }

            # 发送通知
            response = requests.post(
                Config.FORWARD_URL,
                json=forward_data,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )

            if 200 <= response.status_code < 300:
                logger.info(f"AI 降级通知已发送到飞书")
            else:
                logger.warning(f"AI 降级通知发送失败，状态码: {response.status_code}")

    except Exception as e:
        # 降级通知失败不应影响主流程
        logger.error(f"发送 AI 降级通知失败: {str(e)}")


def analyze_with_rules(data: dict[str, Any], source: str) -> AnalysisResult:
    """基于规则的简单分析（AI 降级方案）"""
    # 基础分析结果
    analysis = {
        'source': source,
        'event_type': 'unknown',
        'importance': 'medium',
        'summary': '规则分析（AI 降级）',
        'actions': ['查看告警详情', '检查 AI 服务状态'],
        'risks': ['使用规则分析，可能不够准确']
    }

    # 检测告警格式
    is_prometheus = 'alerts' in data and isinstance(data.get('alerts'), list) and len(data.get('alerts', [])) > 0

    if is_prometheus:
        # Prometheus Alertmanager 格式
        first_alert = data['alerts'][0]
        labels = first_alert.get('labels', {})

        # 获取告警名称
        alert_name = labels.get('alertname', labels.get('alertingRuleName', 'unknown'))
        analysis['event_type'] = alert_name

        # 获取告警级别
        alert_level = labels.get('internal_label_alert_level', labels.get('severity', '')).lower()

        # 判断重要性
        if alert_level in ['critical', 'p0', '严重', 'error']:
            analysis['importance'] = 'high'
            analysis['summary'] = f'🔴 严重告警: {alert_name}'
            analysis['actions'] = ['立即处理', '检查服务状态', '查看日志']
        elif alert_level in ['warning', 'warn', 'p1']:
            analysis['importance'] = 'medium'
            analysis['summary'] = f'🟡 警告告警: {alert_name}'
            analysis['actions'] = ['关注趋势', '准备应对措施']
        else:
            analysis['summary'] = f'📊 告警: {alert_name}'

    else:
        # 华为云/通用格式
        # 获取告警名称
        rule_name = data.get('RuleName') or data.get('alert_name') or data.get('MetricName', 'unknown')
        analysis['event_type'] = rule_name

        # 获取告警级别
        level = str(data.get('Level', '')).lower()

        # 判断重要性
        if level in ['critical', 'error', '严重', 'p0']:
            analysis['importance'] = 'high'
            analysis['summary'] = f'🔴 严重告警: {rule_name}'
            analysis['actions'] = ['立即处理', '检查资源状态', '查看监控指标']
        elif level in ['warn', 'warning', 'p1']:
            analysis['importance'] = 'medium'
            analysis['summary'] = f'🟡 警告告警: {rule_name}'
            analysis['actions'] = ['关注趋势', '评估影响范围']
        else:
            # 检查指标名称中的关键词
            metric_name = str(data.get('MetricName', '')).lower()
            if any(keyword in metric_name for keyword in ['4xxqps', '5xxqps', 'error', 'cpu', 'memory', 'disk']):
                analysis['importance'] = 'medium'
                analysis['summary'] = f'📊 监控告警: {rule_name}'
            else:
                analysis['summary'] = f'ℹ️ 通知: {rule_name}'

        # 检查阈值超标情况
        current_value = data.get('CurrentValue')
        threshold = data.get('Threshold')
        if current_value is not None and threshold is not None:
            try:
                current_num = float(current_value)
                threshold_num = float(threshold)
                if current_num > threshold_num * 4:
                    # 超过4倍阈值，提升重要性
                    analysis['importance'] = 'high'
                    analysis['summary'] = f'🔴 严重超标: {rule_name} (当前值 {current_value} >> 阈值 {threshold})'
            except (ValueError, TypeError):
                pass

        # 检查资源信息
        resources = data.get('Resources', [])
        if resources and isinstance(resources, list):
            resource_count = len(resources)
            if resource_count > 1:
                analysis['impact_scope'] = f'影响 {resource_count} 个资源'

    # 通用事件类型检查（兜底）
    if analysis['event_type'] == 'unknown':
        event = str(data.get('event', data.get('event_type', ''))).lower()
        if event:
            analysis['event_type'] = event

            # 基于关键词判断
            if any(keyword in event for keyword in ['error', 'failure', 'critical', 'alert', '错误', '失败', '故障']):
                analysis['importance'] = 'high'
                analysis['summary'] = f'🔴 严重事件: {event}'
            elif any(keyword in event for keyword in ['warning', 'warn', '警告']):
                analysis['importance'] = 'medium'
                analysis['summary'] = f'🟡 警告事件: {event}'

    return analysis


def forward_to_remote(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    target_url: Optional[str] = None,
    is_periodic_reminder: bool = False
) -> ForwardResult:
    """将分析后的数据转发到远程服务器

    Args:
        webhook_data: Webhook 数据
        analysis_result: AI 分析结果
        target_url: 目标 URL
        is_periodic_reminder: 是否为周期性提醒
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
            forward_data = build_feishu_message(webhook_data, analysis_result, is_periodic_reminder=is_periodic_reminder)
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
        
        if 200 <= response.status_code < 300:
            logger.info(f"成功转发到远程服务器: {target_url} (状态码: {response.status_code})")
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


def build_feishu_message(webhook_data: WebhookData, analysis_result: AnalysisResult, is_periodic_reminder: bool = False) -> dict:
    """构建飞书机器人消息格式

    Args:
        webhook_data: Webhook 数据
        analysis_result: AI 分析结果
        is_periodic_reminder: 是否为周期性提醒
    """
    # 获取基本信息
    source = webhook_data.get('source', 'unknown')
    timestamp = webhook_data.get('timestamp', '')
    importance = analysis_result.get('importance', 'medium')
    summary = analysis_result.get('summary', '无摘要')
    event_type = analysis_result.get('event_type', '未知事件')
    duplicate_count = webhook_data.get('duplicate_count', 1)

    # 使用配置中的重要性配置
    imp_info = Config.IMPORTANCE_CONFIG.get(importance, Config.IMPORTANCE_CONFIG['medium'])

    # 标题：如果是周期性提醒，添加特殊标识
    if is_periodic_reminder:
        title = f"🔔 周期性提醒：告警持续中（已重复 {duplicate_count} 次）"
    else:
        title = "📡 Webhook 事件通知"

    # 构建卡片消息
    card_content = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": title
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
    
    return {
        "msg_type": "interactive",
        "card": card_content
    }
