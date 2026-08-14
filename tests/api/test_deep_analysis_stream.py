"""Relaying the investigator's progress: a window, never a source of truth."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest


class _Upstream:
    def __init__(self, status_code: int, lines: list[str]) -> None:
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self) -> _Upstream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Client:
    def __init__(self, upstream: _Upstream) -> None:
        self.upstream = upstream
        self.calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> _Upstream:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.upstream


async def _drain(response: Any) -> list[dict[str, Any]]:
    chunks = [chunk async for chunk in response.body_iterator]
    text = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks).decode()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_the_stream_is_relayed_and_the_gateway_token_stays_here(monkeypatch) -> None:
    from api.v1 import deep_analysis

    record = SimpleNamespace(gateway_session_key="hook:deep-analysis:grafana:abc", gateway_name="")
    session = SimpleNamespace(get=lambda model, ident: _async(record))
    client = _Client(_Upstream(200, ['{"type":"snapshot","status":"running"}', "", '{"type":"done"}']))

    monkeypatch.setattr(deep_analysis, "get_deep_analysis_client", lambda: client)
    monkeypatch.setattr(
        deep_analysis,
        "resolve_gateway",
        lambda name: SimpleNamespace(name="default", http_api_url="http://probe:8088", gateway_url="", token="tkn"),
    )

    response = await deep_analysis.stream_deep_analysis(7, session=session)  # type: ignore[arg-type]
    messages = await _drain(response)

    assert [m["type"] for m in messages] == ["snapshot", "done"]
    assert client.calls[0]["url"] == "http://probe:8088/v1/runs/hook%3Adeep-analysis%3Agrafana%3Aabc/stream"
    # The gateway credential is used server-side and never handed to the browser.
    assert client.calls[0]["headers"] == {"Authorization": "Bearer tkn"}
    assert "Authorization" not in response.headers


@pytest.mark.asyncio
async def test_a_refused_upstream_ends_the_window_without_failing_the_analysis(monkeypatch) -> None:
    from api.v1 import deep_analysis

    record = SimpleNamespace(gateway_session_key="k", gateway_name="")
    session = SimpleNamespace(get=lambda model, ident: _async(record))
    monkeypatch.setattr(deep_analysis, "get_deep_analysis_client", lambda: _Client(_Upstream(404, [])))
    monkeypatch.setattr(
        deep_analysis,
        "resolve_gateway",
        lambda name: SimpleNamespace(name="default", http_api_url="http://probe:8088", gateway_url="", token=""),
    )

    messages = await _drain(await deep_analysis.stream_deep_analysis(7, session=session))  # type: ignore[arg-type]

    assert messages == [{"type": "error", "detail": "gateway refused the stream"}]


@pytest.mark.asyncio
async def test_an_analysis_with_no_session_has_nothing_to_watch() -> None:
    from api.v1 import deep_analysis

    record = SimpleNamespace(gateway_session_key="", gateway_name="")
    session = SimpleNamespace(get=lambda model, ident: _async(record))

    response = await deep_analysis.stream_deep_analysis(7, session=session)  # type: ignore[arg-type]

    assert response.status_code == 409


async def _async(value: Any) -> Any:
    return value
