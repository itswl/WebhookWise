"""Prompt template loading for AI analysis."""

import asyncio
from pathlib import Path

from core.logger import get_logger
from services.analysis.analysis_policies import PromptPolicy

logger = get_logger("analysis.ai_prompt")

_prompt_template_lock = asyncio.Lock()
_prompt_templates: dict[str, str] = {}
_prompt_sources: dict[str, str] = {}

USER_PROMPT_KIND = "user"
DEEP_ANALYSIS_PROMPT_KIND = "deep_analysis"
INCIDENT_SUMMARY_PROMPT_KIND = "incident_summary"


def get_prompt_source(kind: str = USER_PROMPT_KIND) -> str:
    return _prompt_sources.get(kind, "unknown")


def get_prompt_version(kind: str = USER_PROMPT_KIND) -> str:
    """Return a short content fingerprint of the loaded prompt template.

    Used to namespace the AI analysis cache so editing the prompt invalidates
    stale results. Returns "unloaded" before the template has been read.
    """
    import hashlib

    template = _prompt_templates.get(kind)
    if template is None:
        return "unloaded"
    return hashlib.blake2b(template.encode("utf-8"), digest_size=6).hexdigest()


def resolve_prompt_path(prompt_file: str) -> Path:
    file_path = Path(prompt_file)
    if file_path.is_absolute():
        return file_path
    project_root = Path(__file__).resolve().parents[2]
    return project_root / file_path


async def _load_prompt_template(kind: str, policy: PromptPolicy) -> str:
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
                    logger.warning(
                        "Failed to load prompt template from file kind=%s path=%s error=%s", kind, file_path, e
                    )

        _prompt_sources[kind] = policy.builtin_source
        _prompt_templates[kind] = policy.builtin_prompt
        return policy.builtin_prompt


async def load_user_prompt_template(policy: PromptPolicy | None = None) -> str:
    return await _load_prompt_template(USER_PROMPT_KIND, policy or PromptPolicy.user())


async def load_deep_analysis_prompt_template(policy: PromptPolicy | None = None) -> str:
    return await _load_prompt_template(DEEP_ANALYSIS_PROMPT_KIND, policy or PromptPolicy.deep_analysis())


async def load_incident_summary_prompt_template(policy: PromptPolicy | None = None) -> str:
    return await _load_prompt_template(
        INCIDENT_SUMMARY_PROMPT_KIND,
        policy or PromptPolicy.incident_summary(),
    )


async def reload_user_prompt_template(policy: PromptPolicy | None = None) -> str:
    async with _prompt_template_lock:
        _prompt_templates.pop(USER_PROMPT_KIND, None)
        _prompt_sources.pop(USER_PROMPT_KIND, None)
    return await load_user_prompt_template(policy=policy)


async def reload_deep_analysis_prompt_template(policy: PromptPolicy | None = None) -> str:
    async with _prompt_template_lock:
        _prompt_templates.pop(DEEP_ANALYSIS_PROMPT_KIND, None)
        _prompt_sources.pop(DEEP_ANALYSIS_PROMPT_KIND, None)
    return await load_deep_analysis_prompt_template(policy=policy)


async def reload_incident_summary_prompt_template(policy: PromptPolicy | None = None) -> str:
    async with _prompt_template_lock:
        _prompt_templates.pop(INCIDENT_SUMMARY_PROMPT_KIND, None)
        _prompt_sources.pop(INCIDENT_SUMMARY_PROMPT_KIND, None)
    return await load_incident_summary_prompt_template(policy=policy)
