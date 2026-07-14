"""OpenAI / Instructor client and tracked LLM calls."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

import httpx
import yaml
from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from core.http_client import get_http_client
from core.logger import get_logger, mask_url
from core.observability.attributes import AI_ENGINE, AI_MODEL, AI_PROVIDER, WEBHOOK_SOURCE
from core.observability.metrics import (
    AI_ANALYSIS_DURATION_SECONDS,
    AI_COST_USD_TOTAL,
    AI_REQUESTS_TOTAL,
    AI_TOKENS_TOTAL,
    OPENAI_ERRORS_TOTAL,
    sanitize_source,
)
from core.observability.tracing import otel_span, set_span_error
from schemas.analysis import WebhookAnalysisResult
from services.analysis.ai_errors import is_ai_provider_retryable_error, is_ai_provider_runtime_error
from services.analysis.ai_prompt import get_prompt_source, load_user_prompt_template
from services.analysis.alert_identity_context import build_alert_identity_context
from services.analysis.analysis_policies import AIProviderPolicy
from services.webhooks.payload_sanitizer import sanitize_for_ai_async
from services.webhooks.types import AnalysisResult

logger = get_logger("analysis.ai_llm_client")

if TYPE_CHECKING:
    import instructor

_openai_client_lock = asyncio.Lock()
_openai_client: AsyncOpenAI | None = None
_instructor_client: instructor.Instructor | None = None
_StructuredResultT = TypeVar("_StructuredResultT", bound=BaseModel)


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


def _resolve_instructor_mode(mode_name: str) -> instructor.Mode:
    """Resolve a configured instructor Mode by name, falling back to JSON.

    Lets operators opt into stricter structured-output modes (e.g.
    openrouter_structured_outputs / tools_strict / json_schema) when the
    upstream provider supports them, without betting the deployment on a single
    mode — an unknown or unavailable name safely degrades to Mode.JSON.
    """
    import instructor

    try:
        return instructor.Mode[mode_name.strip().upper()]
    except (KeyError, AttributeError):
        logger.warning("[AI] Unknown instructor mode %r, falling back to JSON", mode_name)
        return instructor.Mode.JSON


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
    import instructor

    global _openai_client, _instructor_client
    policy = policy or AIProviderPolicy.from_config()
    async with _openai_client_lock:
        if _instructor_client is None:
            if _openai_client is None:
                logger.info(
                    "[AI] Initializing OpenAI client model=%s api_url=%s injected_http_client=%s",
                    policy.model,
                    mask_url(policy.api_url),
                    http_client is not None,
                )
                _openai_client = AsyncOpenAI(
                    api_key=policy.api_key,
                    base_url=policy.api_url,
                    http_client=http_client or get_http_client(),
                    timeout=httpx.Timeout(policy.http_timeout_seconds, connect=policy.http_connect_timeout_seconds),
                )
            _instructor_client = instructor.from_openai(
                _openai_client, mode=_resolve_instructor_mode(policy.instructor_mode)
            )
            logger.info("[AI] OpenAI client initialization complete model=%s", policy.model)


async def reset_openai_client() -> None:
    global _openai_client, _instructor_client
    async with _openai_client_lock:
        if _openai_client is not None or _instructor_client is not None:
            logger.info("[AI] Resetting OpenAI client")
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


async def create_structured_completion(
    *,
    response_model: type[_StructuredResultT],
    user_prompt: str,
    source: str,
    system_prompt: str | None = None,
    policy: AIProviderPolicy | None = None,
) -> tuple[_StructuredResultT, int, int]:
    """Run a typed completion through the shared client, breaker and metrics."""
    policy = policy or AIProviderPolicy.from_config()
    if not policy.available:
        raise RuntimeError("AI provider is not configured")
    client = await _get_instructor_client_async()
    metric_source = sanitize_source(source)
    started = time.time()

    async def invoke() -> tuple[_StructuredResultT, _Completion]:
        typed = cast(Any, client)
        return cast(
            tuple[_StructuredResultT, _Completion],
            await typed.chat.completions.create_with_completion(
                model=policy.model,
                response_model=response_model,
                messages=[
                    {"role": "system", "content": system_prompt or policy.system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=policy.temperature,
                max_retries=2,
            ),
        )

    from services.analysis.circuit_breakers import llm_cb

    with otel_span(
        "ai.structured_request",
        {
            WEBHOOK_SOURCE: source,
            AI_MODEL: policy.model,
            AI_PROVIDER: "openai",
            AI_ENGINE: "openai",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": policy.model,
            "ai.response_model": response_model.__name__,
        },
    ) as span:
        try:
            result, completion = await llm_cb.call_async(invoke)
        except Exception as exc:
            AI_REQUESTS_TOTAL.labels(metric_source, "openai", "error").inc()
            set_span_error(span, exc)
            raise

        tokens_in = completion.usage.prompt_tokens if completion.usage else 0
        tokens_out = completion.usage.completion_tokens if completion.usage else 0
        cost = policy.cost_for_tokens(tokens_in, tokens_out)
        AI_REQUESTS_TOTAL.labels(metric_source, "openai", "success").inc()
        AI_TOKENS_TOTAL.labels(policy.model, "input").inc(tokens_in)
        AI_TOKENS_TOTAL.labels(policy.model, "output").inc(tokens_out)
        AI_COST_USD_TOTAL.labels(model=policy.model).inc(cost)
        AI_ANALYSIS_DURATION_SECONDS.labels(source=metric_source, engine="openai").observe(time.time() - started)
        if span is not None:
            span.set_attribute("gen_ai.usage.input_tokens", tokens_in)
            span.set_attribute("gen_ai.usage.output_tokens", tokens_out)
            span.set_attribute("ai.cost.usd", cost)
        return result, tokens_in, tokens_out


def _dump_prompt_yaml(identity_context: dict[str, Any], cleaned_data: dict[str, Any]) -> tuple[str, str]:
    """Serialize the two prompt sections to YAML (run in a worker thread)."""
    identity_yaml = yaml.dump(identity_context, allow_unicode=True, default_flow_style=False, sort_keys=False)
    data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return identity_yaml, data_yaml


def _build_kb_query(source: str, identity_context: dict[str, Any], cleaned_data: dict[str, Any]) -> str:
    """Compose the retrieval query from the most identifying alert fields.

    Uses source + identity (service/resource/rule/metric...) so retrieval keys
    on *what the alert is about*, not boilerplate. Falls back to a short slice of
    the payload text when identity is sparse.
    """
    identity = identity_context.get("identity", {}) if isinstance(identity_context, dict) else {}
    parts = [source]
    if isinstance(identity, dict):
        parts.extend(str(v) for v in identity.values() if v)
    text = " ".join(p for p in parts if p).strip()
    if len(text) < 16:  # identity too sparse — add a bit of payload context
        text = f"{text} {str(cleaned_data)[:300]}".strip()
    return text


async def _retrieve_kb_context(source: str, identity_context: dict[str, Any], cleaned_data: dict[str, Any]) -> str:
    """Best-effort RAG context block for the prompt; '' when disabled/empty/failed.

    Retrieval must never block or fail an alert analysis — any error degrades to
    no context (the analysis simply runs without internal docs).
    """
    from core.app_context import get_config_manager

    if not get_config_manager().kb.KB_ENABLED:
        return ""
    try:
        from db.session import session_scope
        from services.kb.retrieval import retrieve_context

        query = _build_kb_query(source, identity_context, cleaned_data)
        async with session_scope() as session:
            context = await retrieve_context(session, query)
        if not context:
            return ""
        return f"\n**相关内部知识（仅供参考，需结合实际数据判断）**:\n{context}\n"
    except Exception as exc:  # noqa: BLE001 - KB context is best-effort, never a gate
        logger.warning("[KB] context retrieval failed, analyzing without it: %s", exc)
        return ""


async def _build_user_prompt(data: dict[str, Any], source: str, policy: AIProviderPolicy) -> str:
    """Assemble the full user prompt for one alert (sanitize → identity → YAML → KB → template).

    Deterministic for a given alert, and not free: the YAML dumps are CPU-bound
    (offloaded to a thread) and KB retrieval makes an embedding API call. Build
    it once per alert and let only the provider call retry.
    """
    cleaned_data = await sanitize_for_ai_async(data)
    identity_context = build_alert_identity_context(source, cleaned_data)
    # PyYAML's pure-Python emitter is CPU-bound and the payload can be large;
    # offload both dumps to a thread so the worker event loop is not stalled.
    identity_yaml, data_yaml = await asyncio.to_thread(_dump_prompt_yaml, identity_context, cleaned_data)
    kb_context = await _retrieve_kb_context(source, identity_context, cleaned_data)
    user_prompt = (await load_user_prompt_template()).format(
        source=source,
        identity_json=identity_yaml,
        data_json=data_yaml,
        kb_context=kb_context,
    )
    logger.info(
        "[AI] Starting LLM analysis source=%s model=%s sanitized_fields=%s identity_fields=%s prompt_bytes=%s prompt_source=%s",
        source,
        policy.model,
        len(cleaned_data),
        len(identity_context.get("identity", {})),
        len(user_prompt.encode("utf-8")),
        get_prompt_source(),
    )
    return user_prompt


async def _analyze_with_openai_tracked(
    user_prompt: str,
    source: str,
    *,
    policy: AIProviderPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[AnalysisResult, int, int]:
    policy = policy or AIProviderPolicy.from_config()
    client = await _get_instructor_client_async(http_client=http_client)

    with otel_span(
        "ai.request",
        {
            WEBHOOK_SOURCE: source,
            AI_MODEL: policy.model,
            AI_PROVIDER: "openai",
            AI_ENGINE: "openai",
            "gen_ai.system": "openai",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": policy.model,
            "gen_ai.request.temperature": policy.temperature,
            "gen_ai.request.max_retries": 2,
            "prompt.source": get_prompt_source(),
            "prompt.size_bytes": len(user_prompt.encode("utf-8")),
        },
    ) as s:
        try:
            res, completion = await _create_with_completion(
                client, model=policy.model, user_prompt=user_prompt, policy=policy
            )
        except Exception as exc:
            set_span_error(s, exc)
            raise

        t_in = completion.usage.prompt_tokens if completion.usage else 0
        t_out = completion.usage.completion_tokens if completion.usage else 0
        cost = policy.cost_for_tokens(t_in, t_out)
        AI_TOKENS_TOTAL.labels(policy.model, "input").inc(t_in)
        AI_TOKENS_TOTAL.labels(policy.model, "output").inc(t_out)
        AI_COST_USD_TOTAL.labels(model=policy.model).inc(cost)
        if s:
            s.set_attribute("ai.tokens.input", t_in)
            s.set_attribute("ai.tokens.output", t_out)
            s.set_attribute("ai.cost.usd", cost)
            s.set_attribute("gen_ai.response.model", policy.model)
            s.set_attribute("gen_ai.usage.input_tokens", t_in)
            s.set_attribute("gen_ai.usage.output_tokens", t_out)
    logger.info(
        "[AI] LLM analysis complete source=%s model=%s tokens_in=%s tokens_out=%s cost_usd=%.6f",
        source,
        policy.model,
        t_in,
        t_out,
        cost,
    )
    return cast(AnalysisResult, res.to_dict()), t_in, t_out


async def _call_ai_with_retry(
    parsed_data: dict[str, Any], source: str, *, http_client: httpx.AsyncClient | None = None
) -> tuple[AnalysisResult, int, int]:
    # Prompt construction is hoisted out of the retry loop: on a transient
    # provider error the retries below reuse the same prompt instead of paying
    # the sanitize/YAML/KB-embedding cost again for identical input.
    policy = AIProviderPolicy.from_config()
    user_prompt = await _build_user_prompt(parsed_data, source, policy)
    return await _invoke_ai_with_retry(user_prompt, source, policy=policy, http_client=http_client)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=2),
    reraise=True,
    retry=retry_if_exception(is_ai_provider_retryable_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
async def _invoke_ai_with_retry(
    user_prompt: str,
    source: str,
    *,
    policy: AIProviderPolicy | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[AnalysisResult, int, int]:
    start = time.time()
    metric_source = sanitize_source(source)
    try:
        res, t_in, t_out = await _analyze_with_openai_tracked(
            user_prompt, source, policy=policy, http_client=http_client
        )
        AI_REQUESTS_TOTAL.labels(metric_source, "openai", "success").inc()
        AI_ANALYSIS_DURATION_SECONDS.labels(source=metric_source, engine="openai").observe(time.time() - start)
        return res, t_in, t_out
    except Exception as e:
        if not is_ai_provider_runtime_error(e):
            raise
        AI_REQUESTS_TOTAL.labels(metric_source, "openai", "error").inc()
        OPENAI_ERRORS_TOTAL.labels(type=type(e).__name__.lower()).inc()
        logger.warning("[AI] LLM call failed source=%s error_type=%s", source, type(e).__name__)
        raise


async def call_ai_with_breaker(
    parsed_data: dict[str, Any], source: str, *, http_client: httpx.AsyncClient | None = None
) -> tuple[AnalysisResult, int, int]:
    """Run the (retried) LLM analysis behind a circuit breaker.

    Wraps the whole retry sequence as one breaker call, so sustained provider
    failures open the breaker and subsequent alerts fast-fail with
    CircuitBreakerOpenException (the caller degrades to rule analysis) instead of
    each paying the full retry+timeout budget. A policy-refusal (non runtime
    error) propagates without tripping the breaker.
    """
    from services.analysis.circuit_breakers import llm_cb

    return await llm_cb.call_async(_call_ai_with_retry, parsed_data, source, http_client=http_client)
