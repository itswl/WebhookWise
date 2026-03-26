"""飞书 Bot 消息构建器 - 构建飞书卡片格式的回复"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class FeishuCardBuilder:
    """飞书交互卡片构建器"""
    
    @staticmethod
    def build_alert_summary_card(stats: dict) -> dict:
        """构建告警概览卡片
        
        Args:
            stats: 统计数据，包含 total, high, medium, low, recent_1h, unresolved 等
        
        Returns:
            飞书卡片格式的 dict
        """
        total = stats.get('total', 0)
        high = stats.get('high', 0)
        medium = stats.get('medium', 0)
        low = stats.get('low', 0)
        recent_1h = stats.get('recent_1h', 0)
        
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📊 告警概览"},
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**总告警数**\n{total}"}
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**最近1小时**\n{recent_1h}"}
                        }
                    ]
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**🔴 高风险**\n{high}"}
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**🟠 中风险**\n{medium}"}
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**🟢 低风险**\n{low}"}
                        }
                    ]
                },
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": "使用 /analyze <id> 查看详情，/topn 查看频繁告警"}
                    ]
                }
            ]
        }
    
    @staticmethod
    def build_alert_detail_card(alert: dict) -> dict:
        """构建单条告警详情卡片
        
        Args:
            alert: 告警详情数据
        
        Returns:
            飞书卡片格式的 dict
        """
        alert_id = alert.get('id', 'N/A')
        source = alert.get('source', 'unknown')
        importance = alert.get('importance', 'medium')
        timestamp = alert.get('timestamp', '-')
        
        # AI 分析结果
        ai_analysis = alert.get('ai_analysis') or {}
        summary = ai_analysis.get('summary', '暂无分析')
        event_type = ai_analysis.get('event_type', '未知')
        actions = ai_analysis.get('actions', [])
        risks = ai_analysis.get('risks', [])
        suggested_runbook = ai_analysis.get('suggested_runbook', '')
        
        # 重要性颜色映射
        imp_colors = {'high': 'red', 'medium': 'orange', 'low': 'green'}
        imp_emojis = {'high': '🔴', 'medium': '🟠', 'low': '🟢'}
        
        elements = [
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**ID**\n{alert_id}"}
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**来源**\n{source}"}
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**重要性**\n{imp_emojis.get(importance, '⚪')} {importance}"}
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**时间**\n{timestamp[:19] if timestamp else '-'}"}
                    }
                ]
            },
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**📝 事件摘要**\n{summary}"}
            }
        ]
        
        # 添加建议操作
        if actions:
            actions_text = '\n'.join([f"{i+1}. {action}" for i, action in enumerate(actions[:5])])
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**✅ 建议操作**\n{actions_text}"}
            })
        
        # 添加风险提示
        if risks:
            risks_text = '\n'.join([f"• {risk}" for risk in risks[:3]])
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**⚠️ 潜在风险**\n{risks_text}"}
            })
        
        # 添加建议的 Runbook
        if suggested_runbook:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**🔧 建议修复方案**\n`{suggested_runbook}`\n输入 `/fix {alert_id}` 执行修复"}
            })
        
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🔍 告警详情 #{alert_id}"},
                "template": imp_colors.get(importance, 'blue')
            },
            "elements": elements
        }
    
    @staticmethod
    def build_prediction_card(predictions: dict) -> dict:
        """构建预测结果卡片
        
        Args:
            predictions: 预测数据
        
        Returns:
            飞书卡片格式的 dict
        """
        prediction_list = predictions.get('predictions', [])
        
        elements = []
        if not prediction_list:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "暂无预测数据，需要更多历史告警来生成预测。"}
            })
        else:
            for pred in prediction_list[:5]:
                alert_type = pred.get('alert_type', '未知')
                probability = pred.get('probability', 0)
                time_range = pred.get('time_range', '未知时间')
                
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md", 
                        "content": f"• **{alert_type}**\n  概率: {probability*100:.1f}%  |  预计时间: {time_range}"
                    }
                })
        
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🔮 告警预测"},
                "template": "purple"
            },
            "elements": elements
        }
    
    @staticmethod
    def build_remediation_card(execution: dict) -> dict:
        """构建修复执行结果卡片
        
        Args:
            execution: 执行结果数据
        
        Returns:
            飞书卡片格式的 dict
        """
        execution_id = execution.get('execution_id', 'N/A')
        runbook_name = execution.get('runbook_name', 'unknown')
        status = execution.get('status', 'unknown')
        dry_run = execution.get('dry_run', False)
        error_message = execution.get('error_message')
        steps_log = execution.get('steps_log', [])
        
        # 状态颜色映射
        status_colors = {
            'success': 'green',
            'failed': 'red',
            'running': 'blue',
            'awaiting_approval': 'orange',
            'dry_run_complete': 'turquoise'
        }
        status_emojis = {
            'success': '✅',
            'failed': '❌',
            'running': '⏳',
            'awaiting_approval': '⏸️',
            'dry_run_complete': '🧪'
        }
        
        elements = [
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**Runbook**\n{runbook_name}"}
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**状态**\n{status_emojis.get(status, '❓')} {status}"}
                    }
                ]
            }
        ]
        
        # 如果是干运行模式
        if dry_run:
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "🧪 Dry Run 模式，未实际执行"}]
            })
        
        # 添加步骤执行日志
        if steps_log:
            steps_text = ""
            for step in steps_log[:5]:
                step_num = step.get('step', '?')
                action = step.get('action', 'unknown')
                step_status = step.get('status', 'unknown')
                step_emoji = '✅' if step_status == 'success' else '❌'
                steps_text += f"{step_emoji} 步骤 {step_num}: {action}\n"
            
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**执行步骤**\n{steps_text}"}
            })
        
        # 添加错误信息
        if error_message:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**❌ 错误信息**\n{error_message}"}
            })
        
        # 添加执行 ID
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"执行 ID: {execution_id[:16]}..."}]
        })
        
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🔧 修复执行结果"},
                "template": status_colors.get(status, 'blue')
            },
            "elements": elements
        }
    
    @staticmethod
    def build_text_card(title: str, content: str, color: str = "blue") -> dict:
        """构建通用文本卡片
        
        Args:
            title: 卡片标题
            content: 卡片内容（支持 Markdown）
            color: 标题颜色 (blue/red/orange/green/purple/turquoise)
        
        Returns:
            飞书卡片格式的 dict
        """
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color
            },
            "elements": [
                {"tag": "markdown", "content": content}
            ]
        }
    
    @staticmethod
    def build_error_card(error_message: str) -> dict:
        """构建错误提示卡片
        
        Args:
            error_message: 错误信息
        
        Returns:
            飞书卡片格式的 dict
        """
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❌ 执行出错"},
                "template": "red"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**错误信息**\n{error_message}"}
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "输入 /help 查看可用命令"}]
                }
            ]
        }
    
    @staticmethod
    def build_top_alerts_card(alerts: list, n: int = 10) -> dict:
        """构建频繁告警排行卡片
        
        Args:
            alerts: 频繁告警列表，每项包含 alertname, count, importance 等
            n: 显示数量
        
        Returns:
            飞书卡片格式的 dict
        """
        imp_emojis = {'high': '🔴', 'medium': '🟠', 'low': '🟢'}
        
        if not alerts:
            return FeishuCardBuilder.build_text_card(
                f"📈 频繁告警 Top {n}",
                "暂无频繁告警数据",
                "blue"
            )
        
        content = "| 排名 | 告警名称 | 次数 | 重要性 |\n|---|---|---|---|\n"
        for i, alert in enumerate(alerts[:n], 1):
            name = alert.get('alert_name', alert.get('source', 'unknown'))
            count = alert.get('count', 0)
            importance = alert.get('importance', 'medium')
            emoji = imp_emojis.get(importance, '⚪')
            content += f"| {i} | {name} | {count} | {emoji} {importance} |\n"
        
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📈 频繁告警 Top {n}"},
                "template": "blue"
            },
            "elements": [
                {"tag": "markdown", "content": content}
            ]
        }
    
    @staticmethod
    def build_cost_summary_card(usage: dict) -> dict:
        """构建 AI 成本统计卡片
        
        Args:
            usage: 使用统计数据
        
        Returns:
            飞书卡片格式的 dict
        """
        period = usage.get('period', 'day')
        total_calls = usage.get('total_calls', 0)
        route_breakdown = usage.get('route_breakdown', {})
        percentages = usage.get('percentages', {})
        tokens = usage.get('tokens', {})
        cost = usage.get('cost', {})
        efficiency = usage.get('efficiency', {})
        
        period_labels = {'day': '今日', 'week': '本周', 'month': '本月'}
        period_label = period_labels.get(period, period)
        
        content = f"""**{period_label}调用统计**
- 总调用次数: **{total_calls}**
- AI 调用: {route_breakdown.get('ai', 0)} ({percentages.get('ai', 0)}%)
- 规则路由: {route_breakdown.get('rule', 0)} ({percentages.get('rule', 0)}%)
- 缓存命中: {route_breakdown.get('cache', 0)} ({percentages.get('cache', 0)}%)

**Token 使用量**
- 输入: {tokens.get('input', 0):,}
- 输出: {tokens.get('output', 0):,}
- 总计: {tokens.get('total', 0):,}

**成本**
- 实际成本: ${cost.get('total', 0):.4f}
- 预估节省: ${cost.get('saved_estimate', 0):.4f}

**效率**
- 缓存命中率: {efficiency.get('cache_hit_rate', 0):.1f}%
- 规则路由率: {efficiency.get('rule_route_rate', 0):.1f}%
- 节省 AI 调用: {efficiency.get('ai_calls_avoided', 0)} 次
"""
        
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "💰 AI 成本统计"},
                "template": "turquoise"
            },
            "elements": [
                {"tag": "markdown", "content": content}
            ]
        }
    
    @staticmethod
    def build_runbooks_list_card(runbooks: list) -> dict:
        """构建 Runbook 列表卡片
        
        Args:
            runbooks: Runbook 列表
        
        Returns:
            飞书卡片格式的 dict
        """
        if not runbooks:
            return FeishuCardBuilder.build_text_card(
                "📚 可用 Runbook",
                "暂无可用的 Runbook。\n请在 `runbooks/` 目录下添加 YAML 格式的 Runbook 文件。",
                "blue"
            )
        
        content = "| 名称 | 描述 | 触发条件 | 需要审批 |\n|---|---|---|---|\n"
        for rb in runbooks[:15]:
            name = rb.get('name', 'unknown')
            desc = rb.get('description', '-')[:30]
            trigger = rb.get('trigger', {})
            alert_type = trigger.get('alert_type', '-') if trigger else '-'
            require_approval = '是' if rb.get('safety', {}).get('require_approval') else '否'
            content += f"| `{name}` | {desc} | {alert_type} | {require_approval} |\n"
        
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📚 可用 Runbook ({len(runbooks)} 个)"},
                "template": "blue"
            },
            "elements": [
                {"tag": "markdown", "content": content},
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "使用 /fix <告警ID> 触发对应 Runbook"}]
                }
            ]
        }


# 全局单例
feishu_card = FeishuCardBuilder()
