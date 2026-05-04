"""Prompt 模板管理模块

负责加载、缓存和重载 AI 分析的 prompt 模板。
"""

import logging
from pathlib import Path

from core.config import Config

logger = logging.getLogger("webhook_service.ai_prompts")

# 缓存 prompt 模板
_user_prompt_template: str | None = None
# 当前 prompt 模板来源（用于日志追踪）
_user_prompt_source: str = "unknown"


def get_prompt_source() -> str:
    """获取当前 prompt 模板来源描述。"""
    return _user_prompt_source


def load_user_prompt_template() -> str:
    """加载 User Prompt 模板"""
    global _user_prompt_template, _user_prompt_source

    if _user_prompt_template is not None:
        return _user_prompt_template

    if Config.ai.AI_USER_PROMPT:
        _user_prompt_source = "env:AI_USER_PROMPT"
        _user_prompt_template = Config.ai.AI_USER_PROMPT
        return _user_prompt_template

    prompt_file = Config.ai.AI_USER_PROMPT_FILE
    if prompt_file:
        file_path = Path(prompt_file)
        if not file_path.is_absolute():
            file_path = Path(__file__).parent.parent / file_path

        if file_path.exists():
            try:
                with open(file_path, encoding="utf-8") as f:
                    _user_prompt_template = f.read()
                _user_prompt_source = f"file:{file_path}"
                return _user_prompt_template
            except Exception as e:
                logger.warning(f"从文件加载 prompt 模板失败: {e}")

    # 默认模板 (Instructor 会自动处理 JSON Schema，所以这里只需关注业务逻辑)
    _user_prompt_source = "builtin:default"
    _user_prompt_template = """请分析以下 webhook 事件：

**来源**: {source}
**数据内容**:
```yaml
{data_json}
```

请识别事件的类型、严重程度，并提供摘要、影响评估和处理建议。"""

    return _user_prompt_template


def reload_user_prompt_template() -> str:
    """重新加载 User Prompt 模板"""
    global _user_prompt_template, _user_prompt_source
    _user_prompt_template = None
    _user_prompt_source = "unknown"
    return load_user_prompt_template()
