"""Silenced alerts skip the (paid) AI analysis (option B).

A new alert matching an active silence must NOT call the LLM: reuse a prior
analysis if the dedup chain has one, else store an "unknown" placeholder.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from services.dedup import DedupAction, DedupResult
from services.webhooks import pipeline_stages
from services.webhooks.decisioning import SilenceSnapshot
from services.webhooks.types import (
    WebhookProcessContext,
    WebhookRequestContext,
    analysis_route,
)


def _ctx(source: str = "prometheus", parsed: dict[str, Any] | None = None) -> WebhookProcessContext:
    req = WebhookRequestContext(
        client_ip="203.0.113.1",
        source=source,
        payload=b"{}",
        parsed_data=parsed or {"ProjectName": "eve-cn"},
        webhook_full_data={},
    )
    return WebhookProcessContext(
        event_id=1, request_id="r1", metric_source=source, req_ctx=req, alert_hash="h" * 12, dedup_key="d" * 12
    )


def _new_dedup(analysis: dict[str, Any] | None = None) -> DedupResult:
    return DedupResult(action=DedupAction.NEW, analysis=analysis, original_event_id=None)


def _silence(source: str = "prometheus") -> SilenceSnapshot:
    return SilenceSnapshot(id=7, match_source=source)


class _Deps:
    noise_policy = None


@pytest.mark.asyncio
async def test_silenced_new_alert_skips_analysis_unknown() -> None:
    fresh = AsyncMock()
    with (
        patch.object(pipeline_stages, "resolve_dedup", AsyncMock(return_value=_new_dedup())),
        patch.object(pipeline_stages, "get_cached_active_silences", AsyncMock(return_value=[_silence()])),
        patch.object(pipeline_stages, "_run_fresh_analysis", fresh),
    ):
        analysis, noise, dedup = await pipeline_stages.resolve_noise_context(_ctx(), _Deps())

    fresh.assert_not_called()  # the paid LLM path was skipped
    assert analysis_route(analysis) == "silenced_skip"
    assert analysis["importance"] == "unknown"
    assert noise.suppress_forward is False


@pytest.mark.asyncio
async def test_silenced_alert_reuses_prior_analysis() -> None:
    fresh = AsyncMock()
    prior = {"importance": "high", "summary": "GPU mem high"}
    with (
        patch.object(pipeline_stages, "resolve_dedup", AsyncMock(return_value=_new_dedup(analysis=prior))),
        patch.object(pipeline_stages, "get_cached_active_silences", AsyncMock(return_value=[_silence()])),
        patch.object(pipeline_stages, "_run_fresh_analysis", fresh),
    ):
        analysis, _noise, _dedup = await pipeline_stages.resolve_noise_context(_ctx(), _Deps())

    fresh.assert_not_called()
    assert analysis_route(analysis) == "silenced_skip"
    assert analysis["importance"] == "high"
    assert analysis["summary"] == "GPU mem high"


@pytest.mark.asyncio
async def test_non_silenced_alert_runs_fresh_analysis() -> None:
    fresh = AsyncMock(return_value={"importance": "medium", "summary": "x"})
    with (
        patch.object(pipeline_stages, "resolve_dedup", AsyncMock(return_value=_new_dedup())),
        patch.object(
            pipeline_stages, "get_cached_active_silences", AsyncMock(return_value=[_silence(source="grafana")])
        ),
        patch.object(pipeline_stages, "_run_fresh_analysis", fresh),
        patch.object(
            pipeline_stages,
            "compute_noise",
            AsyncMock(return_value=pipeline_stages.NoiseReductionContext("standalone", None, 0.0, False, "", 0, ())),
        ),
    ):
        analysis, _noise, _dedup = await pipeline_stages.resolve_noise_context(_ctx(), _Deps())

    fresh.assert_awaited_once()  # not silenced (source mismatch) → normal analysis
    assert analysis["importance"] == "medium"


@pytest.mark.asyncio
async def test_silence_load_failure_falls_back_to_analysis() -> None:
    fresh = AsyncMock(return_value={"importance": "low", "summary": "x"})
    with (
        patch.object(pipeline_stages, "resolve_dedup", AsyncMock(return_value=_new_dedup())),
        patch.object(pipeline_stages, "get_cached_active_silences", AsyncMock(side_effect=RuntimeError("redis down"))),
        patch.object(pipeline_stages, "_run_fresh_analysis", fresh),
        patch.object(
            pipeline_stages,
            "compute_noise",
            AsyncMock(return_value=pipeline_stages.NoiseReductionContext("standalone", None, 0.0, False, "", 0, ())),
        ),
    ):
        analysis, _noise, _dedup = await pipeline_stages.resolve_noise_context(_ctx(), _Deps())

    # Silence-load failure must not drop the alert; it analyzes normally.
    fresh.assert_awaited_once()
    assert analysis["importance"] == "low"


@pytest.mark.asyncio
async def test_reuse_path_unaffected_by_silence_skip() -> None:
    # An already-deduplicated alert (REUSE) goes through the existing reuse path,
    # not the silence-skip path; silences aren't even loaded.
    silences = AsyncMock()
    with (
        patch.object(
            pipeline_stages,
            "resolve_dedup",
            AsyncMock(
                return_value=DedupResult(
                    action=DedupAction.REUSE, analysis={"importance": "high", "summary": "s"}, original_event_id=5
                )
            ),
        ),
        patch.object(pipeline_stages, "get_cached_active_silences", silences),
        patch.object(pipeline_stages, "log_ai_usage", AsyncMock()),
    ):
        analysis, _noise, _dedup = await pipeline_stages.resolve_noise_context(_ctx(), _Deps())

    silences.assert_not_called()
    assert analysis_route(analysis) == "redis_reuse"
