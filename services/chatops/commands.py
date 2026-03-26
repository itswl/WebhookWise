"""ChatOps 命令执行器 - 实现各种运维命令"""

import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)


class CommandExecutor:
    """命令执行器"""
    
    def execute(self, intent: str, params: dict, context: dict) -> Optional[dict]:
        """根据意图执行命令，返回飞书卡片格式回复"""
        handlers = {
            'query_status': self._handle_status,
            'analyze_alert': self._handle_analyze,
            'deep_analyze': self._handle_deep_analyze,
            'execute_fix': self._handle_fix,
            'view_predictions': self._handle_predictions,
            'top_alerts': self._handle_top_alerts,
            'ai_cost_summary': self._handle_cost,
            'list_runbooks': self._handle_list_runbooks,
            'show_help': self._handle_help,
            'free_query': self._handle_free_query,
        }
        
        handler = handlers.get(intent, self._handle_unknown)
        try:
            return handler(params, context)
        except Exception as e:
            logger.error(f"Command execution error: {intent} - {e}", exc_info=True)
            from .feishu_bot import feishu_card
            return feishu_card.build_error_card(f"命令执行出错: {str(e)}")
    
    def _handle_status(self, params: dict, context: dict) -> dict:
        """查询当前告警概览"""
        from core.models import WebhookEvent, get_session
        from .feishu_bot import feishu_card
        from sqlalchemy import func
        
        try:
            session = get_session()
            try:
                # 总数
                total = session.query(func.count(WebhookEvent.id)).scalar() or 0
                
                # 按重要性统计
                high = session.query(func.count(WebhookEvent.id)).filter(
                    WebhookEvent.importance == 'high'
                ).scalar() or 0
                
                medium = session.query(func.count(WebhookEvent.id)).filter(
                    WebhookEvent.importance == 'medium'
                ).scalar() or 0
                
                low = session.query(func.count(WebhookEvent.id)).filter(
                    WebhookEvent.importance == 'low'
                ).scalar() or 0
                
                # 最近1小时
                one_hour_ago = datetime.now() - timedelta(hours=1)
                recent_1h = session.query(func.count(WebhookEvent.id)).filter(
                    WebhookEvent.timestamp >= one_hour_ago
                ).scalar() or 0
                
                stats = {
                    'total': total,
                    'high': high,
                    'medium': medium,
                    'low': low,
                    'recent_1h': recent_1h
                }
                
                return feishu_card.build_alert_summary_card(stats)
                
            finally:
                session.close()
                
        except Exception as e:
            logger.error(f"查询告警统计失败: {e}")
            return feishu_card.build_error_card(f"查询失败: {str(e)}")
    
    def _handle_analyze(self, params: dict, context: dict) -> dict:
        """分析指定告警"""
        from .feishu_bot import feishu_card
        from core.models import WebhookEvent, get_session
        
        alert_id = params.get('alert_id') or params.get('args', '').strip()
        if not alert_id:
            return feishu_card.build_text_card(
                "需要告警 ID", 
                "请指定要分析的告警 ID，例如：\n`/analyze 123`\n或：分析告警 123",
                "orange"
            )
        
        try:
            alert_id = int(alert_id)
        except ValueError:
            return feishu_card.build_text_card(
                "无效的告警 ID",
                f"`{alert_id}` 不是有效的告警 ID，请输入数字。",
                "orange"
            )
        
        try:
            session = get_session()
            try:
                event = session.query(WebhookEvent).filter_by(id=alert_id).first()
                if not event:
                    return feishu_card.build_text_card(
                        "告警不存在",
                        f"未找到 ID 为 `{alert_id}` 的告警。",
                        "orange"
                    )
                
                alert_data = {
                    'id': event.id,
                    'source': event.source,
                    'importance': event.importance,
                    'timestamp': event.timestamp.isoformat() if event.timestamp else None,
                    'ai_analysis': event.ai_analysis,
                    'is_duplicate': event.is_duplicate,
                    'duplicate_count': event.duplicate_count
                }
                
                return feishu_card.build_alert_detail_card(alert_data)
                
            finally:
                session.close()
                
        except Exception as e:
            logger.error(f"查询告警详情失败: {e}")
            return feishu_card.build_error_card(f"查询失败: {str(e)}")
    
    def _handle_fix(self, params: dict, context: dict) -> dict:
        """触发修复操作"""
        from .feishu_bot import feishu_card
        
        alert_id = params.get('alert_id') or params.get('args', '').strip()
        if not alert_id:
            return feishu_card.build_text_card(
                "需要告警 ID",
                "请指定要修复的告警 ID，例如：\n`/fix 123`",
                "orange"
            )
        
        try:
            alert_id = int(alert_id)
        except ValueError:
            return feishu_card.build_text_card(
                "无效的告警 ID",
                f"`{alert_id}` 不是有效的告警 ID，请输入数字。",
                "orange"
            )
        
        try:
            from core.models import WebhookEvent, get_session
            from services.remediation.engine import remediation_engine
            
            session = get_session()
            try:
                event = session.query(WebhookEvent).filter_by(id=alert_id).first()
                if not event:
                    return feishu_card.build_text_card(
                        "告警不存在",
                        f"未找到 ID 为 `{alert_id}` 的告警。",
                        "orange"
                    )
                
                # 查找匹配的 Runbook
                alert_data = {
                    'parsed_data': event.parsed_data or {},
                    'alert_hash': event.alert_hash
                }
                runbook_name = remediation_engine.find_matching_runbook(alert_data)
                
                if not runbook_name:
                    return feishu_card.build_text_card(
                        "无匹配的 Runbook",
                        f"未找到与告警 `{alert_id}` 匹配的修复方案。\n\n"
                        f"请使用 `/runbooks` 查看可用的 Runbook，或手动处理此告警。",
                        "orange"
                    )
                
                # 执行 Runbook（干运行模式）
                result = remediation_engine.execute_runbook(
                    runbook_name=runbook_name,
                    alert_data=alert_data,
                    dry_run=True,  # 默认使用干运行模式
                    force=False
                )
                
                return feishu_card.build_remediation_card(result)
                
            finally:
                session.close()
                
        except ImportError as e:
            logger.warning(f"Remediation 模块未启用: {e}")
            return feishu_card.build_text_card(
                "功能未启用",
                "修复执行功能尚未启用。请联系管理员配置 Runbook。",
                "orange"
            )
        except Exception as e:
            logger.error(f"执行修复失败: {e}", exc_info=True)
            return feishu_card.build_error_card(f"执行失败: {str(e)}")
    
    def _handle_predictions(self, params: dict, context: dict) -> dict:
        """查看预测结果"""
        from .feishu_bot import feishu_card
        
        # 预测功能可能由其他 agent 实现，这里返回占位信息
        return feishu_card.build_text_card(
            "🔮 告警预测",
            "预测功能正在开发中...\n\n"
            "未来将支持：\n"
            "- 基于历史数据的告警趋势预测\n"
            "- 异常模式识别\n"
            "- 智能预警推荐",
            "purple"
        )
    
    def _handle_top_alerts(self, params: dict, context: dict) -> dict:
        """查看频繁告警 Top N"""
        from .feishu_bot import feishu_card
        from core.models import WebhookEvent, get_session
        from sqlalchemy import func
        
        n = 10  # 默认 Top 10
        args = params.get('args', '').strip()
        if args.isdigit():
            n = min(int(args), 20)  # 最大 20
        
        try:
            session = get_session()
            try:
                # 按来源和 alert_hash 统计，获取出现次数最多的告警
                # 查询最近 7 天的数据
                seven_days_ago = datetime.now() - timedelta(days=7)
                
                results = session.query(
                    WebhookEvent.source,
                    WebhookEvent.importance,
                    func.count(WebhookEvent.id).label('count')
                ).filter(
                    WebhookEvent.timestamp >= seven_days_ago
                ).group_by(
                    WebhookEvent.source,
                    WebhookEvent.importance
                ).order_by(
                    func.count(WebhookEvent.id).desc()
                ).limit(n).all()
                
                alerts = [
                    {
                        'source': r.source,
                        'importance': r.importance or 'medium',
                        'count': r.count
                    }
                    for r in results
                ]
                
                return feishu_card.build_top_alerts_card(alerts, n)
                
            finally:
                session.close()
                
        except Exception as e:
            logger.error(f"查询频繁告警失败: {e}")
            return feishu_card.build_error_card(f"查询失败: {str(e)}")
    
    def _handle_cost(self, params: dict, context: dict) -> dict:
        """查看 AI 成本统计"""
        from .feishu_bot import feishu_card
        from core.models import AIUsageLog, get_session
        from sqlalchemy import func
        
        try:
            session = get_session()
            try:
                # 统计今日数据
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                
                # 总调用次数
                total_calls = session.query(func.count(AIUsageLog.id)).filter(
                    AIUsageLog.timestamp >= today
                ).scalar() or 0
                
                # 各路由类型统计
                route_stats = session.query(
                    AIUsageLog.route_type,
                    func.count(AIUsageLog.id).label('count')
                ).filter(
                    AIUsageLog.timestamp >= today
                ).group_by(AIUsageLog.route_type).all()
                
                route_breakdown = {r.route_type: r.count for r in route_stats}
                
                # AI 调用统计
                ai_stats = session.query(
                    func.sum(AIUsageLog.tokens_in).label('total_tokens_in'),
                    func.sum(AIUsageLog.tokens_out).label('total_tokens_out'),
                    func.sum(AIUsageLog.cost_estimate).label('total_cost')
                ).filter(
                    AIUsageLog.timestamp >= today,
                    AIUsageLog.route_type == 'ai'
                ).first()
                
                # 构建响应
                ai_calls = route_breakdown.get('ai', 0)
                rule_calls = route_breakdown.get('rule', 0)
                cache_calls = route_breakdown.get('cache', 0)
                
                usage_data = {
                    'period': 'day',
                    'total_calls': total_calls,
                    'route_breakdown': {
                        'ai': ai_calls,
                        'rule': rule_calls,
                        'cache': cache_calls
                    },
                    'percentages': {
                        'ai': round(ai_calls / total_calls * 100, 1) if total_calls > 0 else 0,
                        'rule': round(rule_calls / total_calls * 100, 1) if total_calls > 0 else 0,
                        'cache': round(cache_calls / total_calls * 100, 1) if total_calls > 0 else 0
                    },
                    'tokens': {
                        'input': ai_stats.total_tokens_in or 0 if ai_stats else 0,
                        'output': ai_stats.total_tokens_out or 0 if ai_stats else 0,
                        'total': ((ai_stats.total_tokens_in or 0) + (ai_stats.total_tokens_out or 0)) if ai_stats else 0
                    },
                    'cost': {
                        'total': round(ai_stats.total_cost or 0, 4) if ai_stats else 0,
                        'saved_estimate': 0
                    },
                    'efficiency': {
                        'cache_hit_rate': round(cache_calls / total_calls * 100, 1) if total_calls > 0 else 0,
                        'rule_route_rate': round(rule_calls / total_calls * 100, 1) if total_calls > 0 else 0,
                        'ai_calls_avoided': cache_calls + rule_calls
                    }
                }
                
                return feishu_card.build_cost_summary_card(usage_data)
                
            finally:
                session.close()
                
        except Exception as e:
            logger.error(f"查询 AI 成本统计失败: {e}")
            return feishu_card.build_error_card(f"查询失败: {str(e)}")
    
    def _handle_list_runbooks(self, params: dict, context: dict) -> dict:
        """列出可用的 Runbook"""
        from .feishu_bot import feishu_card
        
        try:
            from services.remediation.engine import remediation_engine
            
            runbooks = remediation_engine.parser.list_runbooks()
            result = []
            for rb in runbooks:
                result.append({
                    'name': rb.name,
                    'description': rb.description,
                    'trigger': {
                        'alert_type': rb.trigger.alert_type if rb.trigger else None,
                        'severity': rb.trigger.severity if rb.trigger else []
                    } if rb.trigger else None,
                    'safety': {
                        'require_approval': rb.safety.require_approval if rb.safety else True
                    }
                })
            
            return feishu_card.build_runbooks_list_card(result)
            
        except ImportError as e:
            logger.warning(f"Remediation 模块未启用: {e}")
            return feishu_card.build_text_card(
                "📚 可用 Runbook",
                "修复执行功能尚未启用。",
                "blue"
            )
        except Exception as e:
            logger.error(f"列出 Runbooks 失败: {e}")
            return feishu_card.build_error_card(f"查询失败: {str(e)}")
    
    def _handle_deep_analyze(self, params: dict, context: dict) -> dict:
        """深度分析指定告警"""
        from .feishu_bot import feishu_card
        from core.models import WebhookEvent, get_session
        from services.skills.agent_engine import agent_engine
        
        # 解析参数：/deep <alert_id> [问题]
        args = params.get('args', '').strip()
        if not args:
            return feishu_card.build_text_card(
                "需要告警 ID",
                "请指定要深度分析的告警 ID，例如：\n`/deep 123`\n或：\n`/deep 123 为什么这个 Pod 会重启？`",
                "orange"
            )
        
        # 解析 alert_id 和可选的用户问题
        parts = args.split(' ', 1)
        alert_id_str = parts[0]
        user_question = parts[1] if len(parts) > 1 else None
        
        try:
            alert_id = int(alert_id_str)
        except ValueError:
            return feishu_card.build_text_card(
                "无效的告警 ID",
                f"`{alert_id_str}` 不是有效的告警 ID，请输入数字。",
                "orange"
            )
        
        try:
            session = get_session()
            try:
                event = session.query(WebhookEvent).filter_by(id=alert_id).first()
                if not event:
                    return feishu_card.build_text_card(
                        "告警不存在",
                        f"未找到 ID 为 `{alert_id}` 的告警。",
                        "orange"
                    )
                
                # 获取告警数据
                alert_data = event.raw_data or {}
                if isinstance(alert_data, str):
                    try:
                        import json
                        alert_data = json.loads(alert_data)
                    except json.JSONDecodeError:
                        alert_data = {"raw": alert_data}
                
                # 调用 Agent 引擎进行深度分析
                logger.info(f"ChatOps 触发深度分析: alert_id={alert_id}")
                result = agent_engine.deep_analyze(
                    alert_data=alert_data,
                    user_question=user_question,
                    alert_id=alert_id
                )
                
                if not result.get('success'):
                    error_msg = result.get('error', '未知错误')
                    return feishu_card.build_text_card(
                        "深度分析失败",
                        f"分析过程中发生错误：{error_msg}\n\n请检查 Skill 配置和连接状态。",
                        "red"
                    )
                
                # 构建深度分析报告卡片
                report = result.get('report', {})
                return feishu_card.build_deep_analysis_card(
                    alert_id=alert_id,
                    report=report,
                    rounds_used=result.get('rounds_used', 0),
                    duration_seconds=result.get('duration_seconds', 0),
                    tool_calls_log=result.get('tool_calls_log', [])
                )
                
            finally:
                session.close()
                
        except Exception as e:
            logger.error(f"深度分析失败: {e}", exc_info=True)
            return feishu_card.build_error_card(f"深度分析失败: {str(e)}")
    
    def _handle_help(self, params: dict, context: dict) -> dict:
        """显示帮助信息"""
        from .feishu_bot import feishu_card
        
        help_text = """**可用命令：**

| 命令 | 说明 |
|------|------|
| `/status` | 查看当前告警概览 |
| `/analyze <id>` | 分析指定告警 |
| `/deep <id> [问题]` | 深度分析告警（调用 AI Agent） |
| `/fix <id>` | 触发告警修复（干运行） |
| `/predict` | 查看告警预测 |
| `/topn [N]` | 查看频繁告警 Top N |
| `/cost` | 查看 AI 成本统计 |
| `/runbooks` | 列出修复方案 |
| `/help` | 显示此帮助 |

**也可以直接用自然语言提问：**
- "过去1小时有多少高优先级告警？"
- "帮我分析一下告警 123"
- "最近告警趋势怎么样？"
- "今天 AI 调用量多少？"
"""
        return feishu_card.build_text_card("🤖 ChatOps 帮助", help_text, "blue")
    
    def _handle_free_query(self, params: dict, context: dict) -> dict:
        """自由文本查询 - 使用 AI 回答"""
        from .feishu_bot import feishu_card
        from core.config import Config
        
        raw_text = params.get('raw_text', '')
        
        if not raw_text:
            return feishu_card.build_text_card(
                "请输入问题",
                "请告诉我您想了解什么？\n输入 `/help` 查看可用命令。",
                "blue"
            )
        
        # 检查 API Key
        if not getattr(Config, 'OPENAI_API_KEY', ''):
            return feishu_card.build_text_card(
                "功能未启用",
                "AI 自由问答功能需要配置 OpenAI API Key。\n\n"
                "目前可以使用以下命令：\n"
                "- `/status` 查看告警概览\n"
                "- `/analyze <id>` 分析指定告警\n"
                "- `/help` 查看所有命令",
                "orange"
            )
        
        try:
            # 获取系统上下文（最近告警统计）
            from core.models import WebhookEvent, get_session
            from sqlalchemy import func
            
            session = get_session()
            try:
                # 统计最近24小时告警
                one_day_ago = datetime.now() - timedelta(hours=24)
                
                total = session.query(func.count(WebhookEvent.id)).filter(
                    WebhookEvent.timestamp >= one_day_ago
                ).scalar() or 0
                
                high = session.query(func.count(WebhookEvent.id)).filter(
                    WebhookEvent.timestamp >= one_day_ago,
                    WebhookEvent.importance == 'high'
                ).scalar() or 0
                
                # 最近的告警
                recent_alerts = session.query(WebhookEvent).filter(
                    WebhookEvent.timestamp >= one_day_ago
                ).order_by(WebhookEvent.timestamp.desc()).limit(5).all()
                
                recent_list = [
                    f"- [{a.source}] {a.importance}: {a.ai_analysis.get('summary', '无摘要')[:50] if a.ai_analysis else '未分析'}"
                    for a in recent_alerts
                ]
                
                system_context = f"""过去24小时告警统计:
- 总告警数: {total}
- 高风险告警: {high}

最近告警:
{chr(10).join(recent_list) if recent_list else '暂无告警'}
"""
            finally:
                session.close()
            
            # 加载 Prompt
            import os
            prompt_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                'prompts', 'chatops_analysis.txt'
            )
            
            if os.path.exists(prompt_path):
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    prompt_template = f.read()
                system_prompt = prompt_template.format(
                    system_context=system_context,
                    user_question=raw_text
                )
            else:
                system_prompt = f"""你是一个专业的 AI SRE Copilot，帮助运维工程师分析和处理告警。

当前系统状态:
{system_context}

用户提问: {raw_text}

请用简洁专业的语言回答，使用 Markdown 格式。"""
            
            # 调用 AI
            from openai import OpenAI
            
            client = OpenAI(
                api_key=Config.OPENAI_API_KEY,
                base_url=Config.OPENAI_API_URL
            )
            
            response = client.chat.completions.create(
                model=Config.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "你是一个专业的 AI SRE Copilot，帮助运维工程师分析和处理告警。回答要简洁专业，使用中文。"},
                    {"role": "user", "content": system_prompt}
                ],
                temperature=0.3,
                max_tokens=500
            )
            
            if response.choices and response.choices[0].message.content:
                answer = response.choices[0].message.content.strip()
                return feishu_card.build_text_card("🤖 AI 回答", answer, "blue")
            else:
                return feishu_card.build_error_card("AI 返回空响应")
                
        except Exception as e:
            logger.error(f"自由问答失败: {e}", exc_info=True)
            return feishu_card.build_error_card(f"查询失败: {str(e)}")
    
    def _handle_unknown(self, params: dict, context: dict) -> dict:
        """未识别的命令"""
        from .feishu_bot import feishu_card
        return feishu_card.build_text_card(
            "未识别的命令",
            "抱歉，我没理解你的意思。输入 `/help` 查看可用命令。",
            "orange"
        )


# 全局单例
command_executor = CommandExecutor()
