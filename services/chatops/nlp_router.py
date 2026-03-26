"""NLP 意图识别路由器 - 使用 LLM 理解自然语言运维命令"""

import logging
import json
import os
from typing import Dict, Optional, Any, Tuple

logger = logging.getLogger(__name__)


class NLPRouter:
    """自然语言意图识别和路由"""
    
    # 命令前缀（快速路由，跳过 NLP）
    COMMAND_PREFIX = {
        '/status': 'query_status',
        '/analyze': 'analyze_alert',
        '/fix': 'execute_fix',
        '/predict': 'view_predictions',
        '/topn': 'top_alerts',
        '/help': 'show_help',
        '/runbooks': 'list_runbooks',
        '/cost': 'ai_cost_summary',
    }
    
    def process(self, text: str, context: dict) -> Optional[dict]:
        """处理输入文本，返回回复卡片
        
        Args:
            text: 用户输入文本
            context: 上下文信息（sender, chat_id等）
        
        Returns:
            飞书卡片格式的回复 dict
        """
        if not text:
            return None
        
        # 1. 快速命令路由（以 / 开头）
        intent, params = self._parse_command(text)
        
        # 2. 如果非命令，使用 LLM 做意图识别
        if not intent:
            intent, params = self._nlp_classify(text)
        
        # 3. 路由到命令执行器
        from .commands import command_executor
        return command_executor.execute(intent, params, context)
    
    def _parse_command(self, text: str) -> Tuple[Optional[str], dict]:
        """解析命令格式 /command [args]"""
        text = text.strip()
        if not text.startswith('/'):
            return None, {}
        
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args_text = parts[1] if len(parts) > 1 else ''
        
        intent = self.COMMAND_PREFIX.get(cmd)
        if intent:
            return intent, {'args': args_text, 'raw_text': text}
        
        return None, {}
    
    def _nlp_classify(self, text: str) -> Tuple[str, dict]:
        """使用 LLM 进行意图分类"""
        try:
            # 读取意图识别 Prompt
            prompt_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 
                'prompts', 'chatops_intent.txt'
            )
            
            if os.path.exists(prompt_path):
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    system_prompt = f.read()
            else:
                system_prompt = self._default_intent_prompt()
            
            # 调用 OpenAI（复用已有配置）
            from core.config import Config
            
            if not getattr(Config, 'OPENAI_API_KEY', ''):
                # 没有 API Key，使用关键词匹配降级
                logger.info("OpenAI API Key 未配置，使用关键词匹配")
                return self._keyword_fallback(text)
            
            # 使用 OpenAI 客户端（复用 ai_analyzer 的调用方式）
            from openai import OpenAI
            
            client = OpenAI(
                api_key=Config.OPENAI_API_KEY,
                base_url=Config.OPENAI_API_URL
            )
            
            response = client.chat.completions.create(
                model=Config.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                temperature=0.1,
                max_tokens=200
            )
            
            if response.choices and response.choices[0].message.content:
                result = response.choices[0].message.content.strip()
                logger.debug(f"NLP 分类结果: {result}")
                return self._parse_nlp_result(result, text)
            else:
                logger.warning("NLP classification returned empty response")
                return self._keyword_fallback(text)
                
        except Exception as e:
            logger.error(f"NLP classification error: {e}")
            return self._keyword_fallback(text)
    
    def _keyword_fallback(self, text: str) -> Tuple[str, dict]:
        """关键词降级匹配"""
        text_lower = text.lower()
        
        if any(kw in text_lower for kw in ['状态', '概览', 'status', '多少', '告警数', '统计']):
            return 'query_status', {'raw_text': text}
        elif any(kw in text_lower for kw in ['分析', 'analyze', '原因', '为什么', '排查', '查看']):
            # 尝试提取告警 ID
            alert_id = self._extract_alert_id(text)
            return 'analyze_alert', {'raw_text': text, 'alert_id': alert_id}
        elif any(kw in text_lower for kw in ['修复', 'fix', '解决', '处理', '自愈', '执行']):
            alert_id = self._extract_alert_id(text)
            return 'execute_fix', {'raw_text': text, 'alert_id': alert_id}
        elif any(kw in text_lower for kw in ['预测', 'predict', '趋势', '预警']):
            return 'view_predictions', {'raw_text': text}
        elif any(kw in text_lower for kw in ['top', '排行', '最多', '频繁']):
            return 'top_alerts', {'raw_text': text}
        elif any(kw in text_lower for kw in ['成本', 'cost', '花费', '调用量', '用量']):
            return 'ai_cost_summary', {'raw_text': text}
        elif any(kw in text_lower for kw in ['runbook', '方案', '修复方案']):
            return 'list_runbooks', {'raw_text': text}
        elif any(kw in text_lower for kw in ['帮助', 'help', '怎么用', '命令']):
            return 'show_help', {'raw_text': text}
        else:
            return 'free_query', {'raw_text': text}
    
    def _extract_alert_id(self, text: str) -> Optional[str]:
        """从文本中提取告警 ID"""
        import re
        # 匹配数字 ID
        match = re.search(r'\b(\d+)\b', text)
        if match:
            return match.group(1)
        return None
    
    def _parse_nlp_result(self, result: str, original_text: str) -> Tuple[str, dict]:
        """解析 LLM 返回的意图分类结果"""
        try:
            # 尝试提取 JSON
            import re
            json_match = re.search(r'\{[^{}]*\}', result, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                intent = data.get('intent', 'free_query')
                params = data.get('params', {})
                params['raw_text'] = original_text
                return intent, params
            else:
                logger.warning(f"NLP result not JSON: {result}")
                return self._keyword_fallback(original_text)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse NLP result: {e}")
            return self._keyword_fallback(original_text)
    
    def _default_intent_prompt(self) -> str:
        """默认意图识别 Prompt"""
        return """你是一个运维 ChatOps 助手的意图识别器。
请分析用户输入，返回 JSON 格式的意图分类结果。

可选意图：
- query_status: 查询告警状态/概览
- analyze_alert: 分析特定告警
- execute_fix: 执行修复操作
- view_predictions: 查看预测结果
- top_alerts: 查看热门/频繁告警
- ai_cost_summary: 查看 AI 成本
- list_runbooks: 列出修复方案
- free_query: 自由问答

返回格式：{"intent": "xxx", "params": {"alert_id": null, "time_range": null, "source": null}}

仅返回 JSON，不要其他文字。"""


# 全局单例
nlp_router = NLPRouter()
