"""Prompt template loading for AI analysis."""

import asyncio
from pathlib import Path
from typing import Protocol

from core.logger import get_logger
from services.analysis.ai_policies import AIPromptPolicy, DeepAnalysisPromptPolicy

logger = get_logger("analysis.ai_prompt")

_prompt_template_lock = asyncio.Lock()
_prompt_templates: dict[str, str] = {}
_prompt_sources: dict[str, str] = {}

USER_PROMPT_KIND = "user"
DEEP_ANALYSIS_PROMPT_KIND = "deep_analysis"


class _PromptPolicy(Protocol):
    @property
    def inline_prompt(self) -> str: ...

    @property
    def prompt_file(self) -> str: ...

    @property
    def builtin_prompt(self) -> str: ...

    @property
    def inline_source(self) -> str: ...

    @property
    def builtin_source(self) -> str: ...


def get_prompt_source(kind: str = USER_PROMPT_KIND) -> str:
    return _prompt_sources.get(kind, "unknown")


def resolve_prompt_path(prompt_file: str) -> Path:
    file_path = Path(prompt_file)
    if file_path.is_absolute():
        return file_path
    project_root = Path(__file__).resolve().parents[2]
    return project_root / file_path


async def _load_prompt_template(kind: str, policy: _PromptPolicy) -> str:
    async with _prompt_template_lock:
        cached = _prompt_templates.get(kind)
        if cached is not None:
            return cached

        if policy.inline_prompt:
            _prompt_sources[kind] = policy.inline_source
            _prompt_templates[kind] = policy.inline_prompt
            return policy.inline_prompt

        prompt_file = policy.prompt_file
        if prompt_file:
            file_path = resolve_prompt_path(prompt_file)
            if file_path.exists():
                try:
                    template = file_path.read_text(encoding="utf-8")
                    _prompt_sources[kind] = f"file:{file_path}"
                    _prompt_templates[kind] = template
                    return template
                except OSError as e:
                    logger.warning("从文件加载 prompt 模板失败 kind=%s path=%s error=%s", kind, file_path, e)

        _prompt_sources[kind] = policy.builtin_source
        _prompt_templates[kind] = policy.builtin_prompt
        return policy.builtin_prompt


async def load_user_prompt_template(policy: AIPromptPolicy | None = None) -> str:
    return await _load_prompt_template(USER_PROMPT_KIND, policy or AIPromptPolicy.from_config())


async def load_deep_analysis_prompt_template(policy: DeepAnalysisPromptPolicy | None = None) -> str:
    return await _load_prompt_template(DEEP_ANALYSIS_PROMPT_KIND, policy or DeepAnalysisPromptPolicy.from_config())


async def reload_user_prompt_template(policy: AIPromptPolicy | None = None) -> str:
    async with _prompt_template_lock:
        _prompt_templates.pop(USER_PROMPT_KIND, None)
        _prompt_sources.pop(USER_PROMPT_KIND, None)
    return await load_user_prompt_template(policy=policy)


async def reload_deep_analysis_prompt_template(policy: DeepAnalysisPromptPolicy | None = None) -> str:
    async with _prompt_template_lock:
        _prompt_templates.pop(DEEP_ANALYSIS_PROMPT_KIND, None)
        _prompt_sources.pop(DEEP_ANALYSIS_PROMPT_KIND, None)
    return await load_deep_analysis_prompt_template(policy=policy)
