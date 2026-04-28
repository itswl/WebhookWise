"""Prompt 模板管理模块

负责加载、缓存和重载 AI 分析的 prompt 模板，
以及 JSON 片段提取与修复的辅助函数。
"""

import json
import logging
import re
from pathlib import Path

from core.config import Config

logger = logging.getLogger("webhook_service.ai_prompts")

# 缓存 prompt 模板
_user_prompt_template: str | None = None


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
            # 相对于项目根目录（services的父目录）
            file_path = Path(__file__).parent.parent / file_path

        if file_path.exists():
            try:
                with open(file_path, encoding="utf-8") as f:
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


def _extract_json_payload(text: str) -> str:
    """从响应文本中提取 JSON 片段。"""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text


def _extract_first_json_object(text: str) -> str | None:
    """提取第一个 JSON 对象（允许末尾不完整）。"""
    start = text.find("{")
    if start < 0:
        return None

    stack: list[str] = ["}"]
    in_string = False
    escape = False

    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if not stack or ch != stack[-1]:
                return text[start : i + 1].strip()
            stack.pop()
            if not stack:
                return text[start : i + 1].strip()

    return text[start:].strip()


def _close_truncated_json(candidate: str) -> str:
    """尝试补全被截断的 JSON。"""
    text = candidate.strip()
    if not text:
        return text

    start = text.find("{")
    if start > 0:
        text = text[start:]

    stack: list[str] = []
    in_string = False
    escape = False

    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and ch == stack[-1]:
            stack.pop()

    text = re.sub(r",\s*$", "", text)
    if in_string:
        text += '"'

    while stack:
        text = re.sub(r",\s*$", "", text)
        text += stack.pop()

    return text


def _safe_json_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw.replace("\\n", " ").replace('\\"', '"').strip()
