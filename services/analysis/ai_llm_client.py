"""OpenAI / Instructor client and tracked LLM calls."""

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol, cast

import httpx
import instructor
import yaml
from openai import AsyncOpenAI
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from core.http_client import get_http_client
from core.logger import logger, mask_url
from core.metrics import (
    AI_ANALYSIS_DURATION_SECONDS,
    AI_COST_USD_TOTAL,
    AI_TOKENS_TOTAL,
    OPENAI_ERRORS_TOTAL,
    sanitize_source,
)
from core.otel import span as otel_span
from schemas import WebhookAnalysisResult
from services.analysis.ai_policies import AIProviderPolicy
from services.analysis.ai_prompt import get_prompt_source, load_user_prompt_template
from services.webhooks.payload_sanitizer import sanitize_for_ai_async
from services.webhooks.types import AnalysisResult

_openai_client_lock = asyncio.Lock()
_openai_client: AsyncOpenAI | None = None
_instructor_client: instructor.Instructor | None = None


class _CompletionUsage(Protocol):
    prompt_tokens: int
    completion_tokens: int


class _Completion(Protocol):
    usage: _CompletionUsage | None


class _InstructorCompletions(Protocol):
    async def create_with_completion(
        self,
        *,
        model: str,
        response_model: type[WebhookAnalysisResult],
        messages: Sequence[dict[str, str]],
        temperature: float,
        max_retries: int,
    ) -> tuple[WebhookAnalysisResult, _Completion]: ...


class _InstructorChat(Protocol):
    completions: _InstructorCompletions


class _InstructorClient(Protocol):
    chat: _InstructorChat


def _get_instructor_client() -> instructor.Instructor:
    raise RuntimeError("_get_instructor_client 已弃用，请使用 _get_instructor_client_async")


async def _get_instructor_client_async(*, http_client: httpx.AsyncClient | None = None) -> instructor.Instructor:
    if _instructor_client is not None:
        return _instructor_client
    await initialize_openai_client(http_client=http_client)
    if _instructor_client is None:
        raise RuntimeError("OpenAI client initialization failed")
    return _instructor_client


async def initialize_openai_client(
    policy: AIProviderPolicy | None = None, *, http_client: httpx.AsyncClient | None = None
) -> None:
    global _openai_client, _instructor_client
    policy = policy or AIProviderPolicy.from_config()
    async with _openai_client_lock:
        if _instructor_client is None:
            if _openai_client is None:
                logger.info(
                    "[AI] 初始化 OpenAI 客户端 model=%s api_url=%s injected_http_client=%s",
                    policy.model,
                    mask_url(policy.api_url),
                    http_client is not None,
                )
                _openai_client = AsyncOpenAI(
                    api_key=policy.api_key,
                    base_url=policy.api_url,
                    http_client=http_client or get_http_client(),
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
            _instructor_client = instructor.from_openai(_openai_client, mode=instructor.Mode.JSON)
            logger.info("[AI] OpenAI 客户端初始化完成 model=%s", policy.model)


async def reset_openai_client() -> None:
    global _openai_client, _instructor_client
    async with _openai_client_lock:
        if _openai_client is not None or _instructor_client is not None:
            logger.info("[AI] 重置 OpenAI 客户端")
        _openai_client = _instructor_client = None


async def _create_with_completion(
    client: instructor.Instructor, *, model: str, user_prompt: str, policy: AIProviderPolicy | None = None
) -> tuple[WebhookAnalysisResult, _Completion]:
    policy = policy or AIProviderPolicy.from_config()
    typed = cast(_InstructorClient, client)
    return await typed.chat.completions.create_with_completion(
        model=model,
        response_model=WebhookAnalysisResult,
        messages=[
            {"role": "system", "content": policy.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=policy.temperature,
        max_retries=2,
    )


async def _analyze_with_openai_tracked(
    data: dict[str, Any],
    source: str,
    *,
    policy: AIProviderPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[AnalysisResult, int, int]:
    policy = policy or AIProviderPolicy.from_config()
    client = await _get_instructor_client_async(http_client=http_client)
    cleaned_data = await sanitize_for_ai_async(data)
    data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    user_prompt = (await load_user_prompt_template()).format(source=source, data_json=data_yaml)
    logger.info(
        "[AI] 开始 LLM 分析 source=%s model=%s sanitized_fields=%s prompt_bytes=%s prompt_source=%s",
        source,
        policy.model,
        len(cleaned_data),
        len(user_prompt.encode("utf-8")),
        get_prompt_source(),
    )

    with otel_span("ai.openai_call", {"source": source, "model": policy.model}) as s:
        res, completion = await _create_with_completion(
            client, model=policy.model, user_prompt=user_prompt, policy=policy
        )

        t_in = completion.usage.prompt_tokens if completion.usage else 0
        t_out = completion.usage.completion_tokens if completion.usage else 0
        cost = policy.cost_for_tokens(t_in, t_out)
        AI_TOKENS_TOTAL.labels(policy.model, "input").inc(t_in)
        AI_TOKENS_TOTAL.labels(policy.model, "output").inc(t_out)
        AI_COST_USD_TOTAL.labels(model=policy.model).inc(cost)
        if s:
            s.set_attribute("tokens_in", t_in)
            s.set_attribute("tokens_out", t_out)
    logger.info(
        "[AI] LLM 分析完成 source=%s model=%s tokens_in=%s tokens_out=%s cost_usd=%.6f",
        source,
        policy.model,
        t_in,
        t_out,
        cost,
    )
    return res.to_dict(), t_in, t_out


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=2),
    reraise=True,
    retry=retry_if_exception(
        lambda e: isinstance(e, (httpx.RequestError, httpx.TimeoutException, ConnectionError, TimeoutError))
    ),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
async def _call_ai_with_retry(
    parsed_data: dict[str, Any], source: str, *, http_client: httpx.AsyncClient | None = None
) -> tuple[dict[str, Any], int, int]:
    start = time.time()
    try:
        res, t_in, t_out = await _analyze_with_openai_tracked(parsed_data, source, http_client=http_client)
        AI_ANALYSIS_DURATION_SECONDS.labels(source=sanitize_source(source), engine="openai").observe(
            time.time() - start
        )
        return res, t_in, t_out
    except Exception as e:
        OPENAI_ERRORS_TOTAL.labels(type=type(e).__name__.lower()).inc()
        logger.warning("[AI] LLM 调用失败 source=%s error_type=%s", source, type(e).__name__)
        raise
