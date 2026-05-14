"""Prompt template loading for AI analysis."""

import asyncio
from pathlib import Path

from core.logger import logger
from services.analysis.ai_policies import AIPromptPolicy

_prompt_template_lock = asyncio.Lock()
_user_prompt_template: str | None = None
_user_prompt_source: str = "unknown"


def get_prompt_source() -> str:
    return _user_prompt_source


def _resolve_prompt_path(prompt_file: str) -> Path:
    file_path = Path(prompt_file)
    if file_path.is_absolute():
        return file_path
    project_root = Path(__file__).resolve().parents[2]
    return project_root / file_path


async def load_user_prompt_template(policy: AIPromptPolicy | None = None) -> str:
    global _user_prompt_template, _user_prompt_source
    policy = policy or AIPromptPolicy.from_config()
    async with _prompt_template_lock:
        if _user_prompt_template is not None:
            return _user_prompt_template

        if policy.inline_prompt:
            _user_prompt_source, _user_prompt_template = "env:AI_USER_PROMPT", policy.inline_prompt
            return _user_prompt_template

        prompt_file = policy.prompt_file
        if prompt_file:
            file_path = _resolve_prompt_path(prompt_file)
            if file_path.exists():
                try:
                    with open(file_path, encoding="utf-8") as f:
                        _user_prompt_template = f.read()
                    _user_prompt_source = f"file:{file_path}"
                    return _user_prompt_template
                except Exception as e:
                    logger.warning("从文件加载 prompt 模板失败: %s", e)

        _user_prompt_source = "builtin:default"
        _user_prompt_template = policy.builtin_prompt
        return _user_prompt_template


async def reload_user_prompt_template(policy: AIPromptPolicy | None = None) -> str:
    global _user_prompt_template
    async with _prompt_template_lock:
        _user_prompt_template = None
    return await load_user_prompt_template(policy=policy)
