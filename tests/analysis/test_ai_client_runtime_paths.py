from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.helpers.metric_helpers import MetricValueCall, StubMetric


class _Policy:
    model = "gpt-test"
    api_key = "test-key"
    api_url = "https://api.example/v1"
    http_timeout_seconds = 12
    http_connect_timeout_seconds = 3
    system_prompt = "You are testing."
    temperature = 0.2
    instructor_mode = "json"

    def cost_for_tokens(self, tokens_in: int, tokens_out: int) -> float:
        return round((tokens_in + tokens_out) * 0.001, 6)


class ProviderBadRequestError(Exception):
    status_code = 400


ProviderBadRequestError.__module__ = "openai._exceptions"


@pytest.fixture
def ai_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[MetricValueCall]]:
    from services.analysis import ai_llm_client

    metric_calls: list[MetricValueCall] = []
    for attr in (
        "AI_ANALYSIS_DURATION_SECONDS",
        "AI_COST_USD_TOTAL",
        "AI_REQUESTS_TOTAL",
        "AI_TOKENS_TOTAL",
        "OPENAI_ERRORS_TOTAL",
    ):
        monkeypatch.setattr(ai_llm_client, attr, StubMetric(metric_calls, attr, record_action=False))
    monkeypatch.setattr(ai_llm_client.AIProviderPolicy, "from_config", staticmethod(lambda: _Policy()))
    monkeypatch.setattr(ai_llm_client, "sanitize_source", lambda source: f"safe:{source}")
    return ai_llm_client, metric_calls


@pytest.mark.asyncio
async def test_initialize_reset_and_get_instructor_client(
    monkeypatch: pytest.MonkeyPatch,
    ai_runtime: tuple[Any, list[object]],
) -> None:
    ai_llm_client, _metric_calls = ai_runtime
    created: dict[str, object] = {}

    class AsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            created["openai"] = kwargs

    class _Mode:
        JSON = "json"

        def __class_getitem__(cls, name: str) -> str:
            # Mimic instructor.Mode[NAME] enum lookup used by _resolve_instructor_mode.
            return {"JSON": cls.JSON}[name]

    class InstructorModule:
        Mode = _Mode

        @staticmethod
        def from_openai(client: object, *, mode: object) -> object:
            created["instructor_source"] = client
            created["mode"] = mode
            return SimpleNamespace(name="instructor")

    monkeypatch.setitem(__import__("sys").modules, "instructor", InstructorModule)
    monkeypatch.setattr(ai_llm_client, "AsyncOpenAI", AsyncOpenAI)
    monkeypatch.setattr(ai_llm_client, "get_http_client", lambda: "shared-http-client")
    ai_llm_client._openai_client = None
    ai_llm_client._instructor_client = None

    await ai_llm_client.initialize_openai_client(policy=_Policy())
    client = await ai_llm_client._get_instructor_client_async()

    assert client == SimpleNamespace(name="instructor")
    assert created["mode"] == "json"
    assert created["openai"]["base_url"] == "https://api.example/v1"
    assert created["openai"]["http_client"] == "shared-http-client"

    await ai_llm_client.reset_openai_client()
    assert ai_llm_client._openai_client is None
    assert ai_llm_client._instructor_client is None


@pytest.mark.asyncio
async def test_analyze_with_openai_tracks_prompt_usage_cost_and_span_attrs(
    monkeypatch: pytest.MonkeyPatch,
    ai_runtime: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], object]]],
) -> None:
    ai_llm_client, metric_calls = ai_runtime
    span_attrs: dict[str, object] = {}
    prompts: list[str] = []

    class Span:
        def set_attribute(self, key: str, value: object) -> None:
            span_attrs[key] = value

    @contextmanager
    def fake_span(_name: str, attrs: dict[str, object]) -> Any:
        span_attrs.update(attrs)
        yield Span()

    async def sanitize_for_ai_async(data: dict[str, object]) -> dict[str, object]:
        return {"safe": data["message"]}

    async def load_user_prompt_template() -> str:
        return "source={source}\nidentity={identity_json}\ndata={data_json}"

    async def create_with_completion(
        _client: object, *, model: str, user_prompt: str, policy: object
    ) -> tuple[object, object]:
        prompts.append(user_prompt)
        assert model == "gpt-test"
        assert isinstance(policy, _Policy)
        result = SimpleNamespace(to_dict=lambda: {"summary": "ok", "severity": "info"})
        usage = SimpleNamespace(prompt_tokens=12, completion_tokens=5)
        return result, SimpleNamespace(usage=usage)

    async def get_instructor_client_async(http_client: object = None) -> object:
        return SimpleNamespace(http_client=http_client)

    monkeypatch.setattr(ai_llm_client, "_get_instructor_client_async", get_instructor_client_async)
    monkeypatch.setattr(ai_llm_client, "sanitize_for_ai_async", sanitize_for_ai_async)
    monkeypatch.setattr(ai_llm_client, "load_user_prompt_template", load_user_prompt_template)
    monkeypatch.setattr(ai_llm_client, "get_prompt_source", lambda: "db")
    monkeypatch.setattr(ai_llm_client, "_create_with_completion", create_with_completion)
    monkeypatch.setattr(ai_llm_client, "otel_span", fake_span)

    result, tokens_in, tokens_out = await ai_llm_client._analyze_with_openai_tracked(
        {"message": "hello"}, "prometheus", policy=_Policy()
    )

    assert result == {"summary": "ok", "severity": "info"}
    assert (tokens_in, tokens_out) == (12, 5)
    assert "source=prometheus" in prompts[0]
    assert "identity:" in prompts[0]
    assert "safe: hello" in prompts[0]
    assert span_attrs["ai.tokens.input"] == 12
    assert span_attrs["ai.cost.usd"] == 0.017
    assert ("AI_TOKENS_TOTAL", ("gpt-test", "input"), {}, 12) in metric_calls
    assert ("AI_TOKENS_TOTAL", ("gpt-test", "output"), {}, 5) in metric_calls
    assert ("AI_COST_USD_TOTAL", (), {"model": "gpt-test"}, 0.017) in metric_calls


@pytest.mark.asyncio
async def test_analyze_with_openai_marks_span_error_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
    ai_runtime: tuple[Any, list[object]],
) -> None:
    ai_llm_client, _metric_calls = ai_runtime
    marked: list[str] = []

    @contextmanager
    def fake_span(_name: str, _attrs: dict[str, object]) -> Any:
        yield "span"

    async def fail_completion(*_args: object, **_kwargs: object) -> tuple[object, object]:
        raise RuntimeError("llm failed")

    async def get_instructor_client_async(http_client: object = None) -> object:
        return SimpleNamespace(http_client=http_client)

    async def sanitize_for_ai_async(data: dict[str, object]) -> dict[str, object]:
        return data

    async def load_user_prompt_template() -> str:
        return "source={source} data={data_json}"

    monkeypatch.setattr(ai_llm_client, "_get_instructor_client_async", get_instructor_client_async)
    monkeypatch.setattr(ai_llm_client, "sanitize_for_ai_async", sanitize_for_ai_async)
    monkeypatch.setattr(ai_llm_client, "load_user_prompt_template", load_user_prompt_template)
    monkeypatch.setattr(ai_llm_client, "get_prompt_source", lambda: "file")
    monkeypatch.setattr(ai_llm_client, "_create_with_completion", fail_completion)
    monkeypatch.setattr(ai_llm_client, "otel_span", fake_span)
    monkeypatch.setattr(ai_llm_client, "set_span_error", lambda _span, err: marked.append(str(err)))

    with pytest.raises(RuntimeError, match="llm failed"):
        await ai_llm_client._analyze_with_openai_tracked({"message": "hello"}, "grafana", policy=_Policy())

    assert marked == ["llm failed"]


@pytest.mark.asyncio
async def test_call_ai_with_retry_records_success_and_non_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
    ai_runtime: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], object]]],
) -> None:
    ai_llm_client, metric_calls = ai_runtime

    async def analyze_success(
        _data: dict[str, object], source: str, *, http_client: httpx.AsyncClient | None = None
    ) -> tuple[dict[str, object], int, int]:
        assert source == "source-a"
        assert http_client is None
        return {"summary": "ok"}, 3, 4

    monkeypatch.setattr(ai_llm_client, "_analyze_with_openai_tracked", analyze_success)
    assert await ai_llm_client._call_ai_with_retry({"a": 1}, "source-a") == ({"summary": "ok"}, 3, 4)

    async def analyze_error(
        _data: dict[str, object], _source: str, *, http_client: httpx.AsyncClient | None = None
    ) -> tuple[dict[str, object], int, int]:
        raise ProviderBadRequestError("bad payload")

    monkeypatch.setattr(ai_llm_client, "_analyze_with_openai_tracked", analyze_error)
    with pytest.raises(ProviderBadRequestError, match="bad payload"):
        await ai_llm_client._call_ai_with_retry({"a": 1}, "source-b")

    assert ("AI_REQUESTS_TOTAL", ("safe:source-a", "openai", "success"), {}, 1) in metric_calls
    assert ("AI_REQUESTS_TOTAL", ("safe:source-b", "openai", "error"), {}, 1) in metric_calls
    assert ("OPENAI_ERRORS_TOTAL", (), {"type": "providerbadrequesterror"}, 1) in metric_calls
