"""AI Agent 编排引擎 - 使用 LLM Function Calling 自动编排多平台查询进行深度分析"""

import json
import logging
import time
import concurrent.futures
from datetime import datetime
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class AgentEngine:
    """AI Agent 深度分析引擎
    
    核心流程：
    1. 接收告警数据 + 用户问题（可选）
    2. 构建 System Prompt + 告警上下文
    3. 调用 LLM（带 tools 参数，包含所有已注册 Skill 的能力）
    4. 如果 LLM 返回 tool_calls → 并行执行对应 Skill
    5. 将执行结果反馈给 LLM
    6. LLM 判断：需要更多信息？→ 继续调用 / 信息足够？→ 生成报告
    7. 最多 N 轮迭代（AGENT_MAX_ROUNDS），防止无限循环
    """
    
    def __init__(self):
        from .base import skill_registry
        self.registry = skill_registry
    
    def deep_analyze(self, alert_data: dict, user_question: str = None, 
                     alert_id: int = None) -> dict:
        """执行深度分析
        
        Args:
            alert_data: 告警原始数据（raw_payload 或 parsed_data）
            user_question: 用户的附加问题（可选）
            alert_id: 告警 ID（用于关联）
        
        Returns:
            {
                "success": bool,
                "report": {
                    "root_cause": str,
                    "evidence": [{"source": str, "finding": str}],
                    "impact": str,
                    "timeline": [{"time": str, "event": str}],
                    "recommendations": [str],
                    "confidence": float
                },
                "tool_calls_log": [{"round": int, "tool": str, "result_summary": str}],
                "rounds_used": int,
                "duration_seconds": float,
                "error": str or None
            }
        """
        from core.config import Config
        
        start_time = time.time()
        max_rounds = Config.AGENT_MAX_ROUNDS
        tool_calls_log = []
        
        # 1. 获取所有可用的 tools
        tools = self.registry.get_all_capabilities()
        if not tools:
            return self._no_tools_report(alert_data)
        
        # 2. 构建初始消息
        messages = self._build_initial_messages(alert_data, user_question)
        
        # 3. 多轮迭代
        for round_num in range(1, max_rounds + 1):
            logger.info(f"Deep analysis round {round_num}/{max_rounds}")
            
            try:
                # 调用 LLM（带 tools）
                response = self._call_llm(messages, tools)
                
                if response is None:
                    return self._error_report("LLM 调用失败", tool_calls_log, start_time)
                
                # 检查是否有 tool_calls
                tool_calls = self._extract_tool_calls(response)
                
                if tool_calls:
                    # 并行执行所有 tool calls
                    results = self._execute_tool_calls(tool_calls, round_num, tool_calls_log)
                    
                    # 将 assistant 消息和 tool 结果加入对话
                    messages.append(response)  # assistant 的 tool_calls 消息
                    messages.extend(results)   # tool 结果消息
                    continue
                else:
                    # LLM 返回纯文本 → 分析完成
                    content = self._extract_content(response)
                    report = self._parse_analysis_report(content)
                    report['tool_calls_log'] = tool_calls_log
                    report['rounds_used'] = round_num
                    report['duration_seconds'] = round(time.time() - start_time, 2)
                    return report
                    
            except Exception as e:
                logger.error(f"Deep analysis round {round_num} error: {e}")
                return self._error_report(str(e), tool_calls_log, start_time)
        
        # 超过最大轮次，强制要求总结
        return self._force_summarize(messages, tools, tool_calls_log, start_time)
    
    def _build_initial_messages(self, alert_data: dict, user_question: str = None) -> list:
        """构建初始对话消息"""
        import os
        
        # 加载 System Prompt
        prompt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                   'prompts', 'deep_analysis.txt')
        if os.path.exists(prompt_path):
            with open(prompt_path, 'r', encoding='utf-8') as f:
                system_prompt = f.read()
        else:
            system_prompt = self._default_system_prompt()
        
        # 构建用户消息
        user_content = f"## 告警数据\n```json\n{json.dumps(alert_data, ensure_ascii=False, indent=2, default=str)}\n```"
        
        if user_question:
            user_content += f"\n\n## 用户问题\n{user_question}"
        
        user_content += "\n\n请分析此告警，调用相关平台工具收集信息，然后给出深度根因分析报告。"
        
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
    
    def _call_llm(self, messages: list, tools: list) -> Optional[dict]:
        """调用 LLM（带 tools/function calling）
        
        使用项目已有的 OpenAI 配置，复用 ai_analyzer.py 中的 API 调用方式
        """
        import requests
        from core.config import Config
        
        api_key = getattr(Config, 'OPENAI_API_KEY', '')
        api_base = getattr(Config, 'OPENAI_API_URL', 'https://api.openai.com/v1')
        model = getattr(Config, 'OPENAI_MODEL', 'gpt-4')
        
        if not api_key:
            logger.error("Deep analysis failed: OPENAI_API_KEY not configured")
            return None
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        
        # 只在有 tools 且第一轮或有 tool_calls 时传 tools
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        
        try:
            timeout = getattr(Config, 'AGENT_TIMEOUT', 120)
            resp = requests.post(
                f"{api_base}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout
            )
            
            if resp.status_code != 200:
                logger.error(f"LLM API error: {resp.status_code} - {resp.text[:500]}")
                return None
            
            data = resp.json()
            choice = data.get('choices', [{}])[0]
            return choice.get('message', {})
            
        except requests.exceptions.Timeout:
            logger.error(f"LLM API timeout ({timeout}s)")
            return None
        except Exception as e:
            logger.error(f"LLM API call failed: {e}")
            return None
    
    def _extract_tool_calls(self, response: dict) -> list:
        """从 LLM 响应中提取 tool_calls"""
        return response.get('tool_calls', [])
    
    def _extract_content(self, response: dict) -> str:
        """从 LLM 响应中提取文本内容"""
        return response.get('content', '') or ''
    
    def _execute_tool_calls(self, tool_calls: list, round_num: int, 
                            tool_calls_log: list) -> list:
        """并行执行多个 tool calls，返回 tool result 消息列表"""
        results = []
        
        # 使用线程池并行执行
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_map = {}
            
            for tc in tool_calls:
                func = tc.get('function', {})
                func_name = func.get('name', '')
                
                try:
                    arguments = json.loads(func.get('arguments', '{}'))
                except json.JSONDecodeError:
                    arguments = {}
                
                tc_id = tc.get('id', '')
                logger.info(f"Round {round_num}: Calling {func_name}({json.dumps(arguments, ensure_ascii=False)[:200]})")
                
                future = executor.submit(self.registry.route_tool_call, func_name, arguments)
                future_map[future] = (tc_id, func_name, arguments)
            
            for future in concurrent.futures.as_completed(future_map):
                tc_id, func_name, arguments = future_map[future]
                
                try:
                    result = future.result(timeout=30)
                except Exception as e:
                    result = {"success": False, "error": str(e)}
                
                # 记录日志
                result_summary = str(result.get('data', result.get('error', '')))[:200]
                tool_calls_log.append({
                    "round": round_num,
                    "tool": func_name,
                    "params": arguments,
                    "success": result.get('success', False),
                    "result_summary": result_summary
                })
                
                logger.info(f"Round {round_num}: {func_name} -> success={result.get('success')}")
                
                # 构建 tool result 消息
                results.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:8000]  # 限制大小
                })
        
        return results
    
    def _parse_analysis_report(self, content: str) -> dict:
        """解析 LLM 返回的分析报告"""
        # 尝试 JSON 解析
        try:
            # 找到 JSON 块
            if '```json' in content:
                json_str = content.split('```json')[1].split('```')[0].strip()
            elif '```' in content:
                json_str = content.split('```')[1].split('```')[0].strip()
            else:
                json_str = content
            
            report = json.loads(json_str)
            return {"success": True, "report": report, "error": None}
        except (json.JSONDecodeError, IndexError):
            pass
        
        # JSON 解析失败，构建文本报告
        return {
            "success": True,
            "report": {
                "root_cause": content,
                "evidence": [],
                "impact": "需要进一步评估",
                "timeline": [],
                "recommendations": [],
                "confidence": 0.5,
                "raw_analysis": content
            },
            "error": None
        }
    
    def _force_summarize(self, messages: list, tools: list, 
                         tool_calls_log: list, start_time: float) -> dict:
        """超过最大轮次，强制要求 LLM 总结"""
        logger.warning("Max rounds reached, forcing summary")
        
        messages.append({
            "role": "user",
            "content": "你已经收集了足够的信息。请立即根据已有信息生成最终的深度分析报告。按照要求的 JSON 格式输出。"
        })
        
        # 不传 tools，强制文本输出
        response = self._call_llm(messages, tools=[])
        if response:
            content = self._extract_content(response)
            report = self._parse_analysis_report(content)
        else:
            report = self._error_report("强制总结失败", tool_calls_log, start_time)
        
        report['tool_calls_log'] = tool_calls_log
        report['rounds_used'] = getattr(self, '_max_rounds', 3)
        report['duration_seconds'] = round(time.time() - start_time, 2)
        return report
    
    def _no_tools_report(self, alert_data: dict) -> dict:
        """没有可用 tools 时的报告"""
        return {
            "success": False,
            "report": None,
            "tool_calls_log": [],
            "rounds_used": 0,
            "duration_seconds": 0,
            "error": "没有可用的 Skill 连接器。请检查 Skill 配置和连接状态。"
        }
    
    def _error_report(self, error: str, tool_calls_log: list, start_time: float) -> dict:
        """错误报告"""
        return {
            "success": False,
            "report": None,
            "tool_calls_log": tool_calls_log,
            "rounds_used": 0,
            "duration_seconds": round(time.time() - start_time, 2),
            "error": error
        }
    
    def _default_system_prompt(self) -> str:
        """默认 System Prompt"""
        return """你是一个专业的 SRE AI Agent，负责对告警进行深度根因分析。

你可以调用各种平台工具来收集信息（Kubernetes、Prometheus、Grafana、日志平台等）。

## 分析策略
1. 先理解告警的基本信息（来源、类型、严重度）
2. 根据告警类型决定需要查询哪些平台
3. 从多个维度收集证据（指标、日志、K8s 状态等）
4. 如果第一轮信息不够，可以发起更深入的查询
5. 综合所有证据，得出根因结论

## 输出格式
最终分析报告使用 JSON 格式：
```json
{
    "root_cause": "根因分析描述",
    "evidence": [{"source": "数据来源", "finding": "发现内容"}],
    "impact": "影响范围评估",
    "timeline": [{"time": "时间", "event": "事件"}],
    "recommendations": ["修复建议1", "修复建议2"],
    "confidence": 0.85
}
```"""


# 全局单例
agent_engine = AgentEngine()
