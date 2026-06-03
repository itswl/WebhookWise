from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from contracts.webhook_payload import webhook_data_from_mapping
from services.dedup import DedupAction, DedupResult
from services.webhooks.command_service import SaveWebhookResult
from services.webhooks.decisioning import ForwardDecision, ForwardRuleSnapshot
from services.webhooks.forwarding_stage import FinalizeAnalysisResult
from services.webhooks.types import (
    NoiseReductionContext,
    WebhookProcessContext,
    WebhookRequestContext,
)
from tests.helpers.metric_helpers import MetricCall, StubMetric


class _Span:
    def __init__(self) -> None:
        self.attrs: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value


@dataclass
class _GateResult:
    suppressed: bool = False
    queue_size: int = 0
    reason: str = ""


@contextmanager
def _span_context(_name: str, _attrs: dict[str, object]):
    yield _Span()


def _ctx() -> WebhookProcessContext:
    req_ctx = WebhookRequestContext(
        client_ip="127.0.0.1",
        source="prometheus",
        payload=b'{"RuleName":"HighCPU"}',
        parsed_data=webhook_data_from_mapping({"RuleName": "HighCPU", "Level": "critical"}),
        webhook_full_data=webhook_data_from_mapping(
            {
                "source": "prometheus",
                "headers": {"authorization": "secret"},
                "parsed_data": {"RuleName": "HighCPU", "Level": "critical"},
            }
        ),
        headers={"authorization": "secret"},
    )
    return WebhookProcessContext(
        event_id=None,
        request_id="req-1",
        metric_source="prometheus",
        req_ctx=req_ctx,
        alert_hash="a" * 64,
        dedup_key="d" * 64,
    )


@pytest.fixture
def patched_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]]:
    from services.webhooks import pipeline

    metric_calls: list[MetricCall] = []
    for name in (
        "WEBHOOK_PIPELINE_STEP_DURATION_SECONDS",
        "WEBHOOK_PIPELINE_STEP_TOTAL",
        "WEBHOOK_PROCESSING_DURATION_SECONDS",
        "WEBHOOK_PROCESSING_STATUS_TOTAL",
        "WEBHOOK_RECEIVED_TOTAL",
        "WEBHOOK_STORM_SUPPRESSED_TOTAL",
    ):
        monkeypatch.setattr(pipeline, name, StubMetric(metric_calls, name))
    monkeypatch.setattr(pipeline, "otel_span", _span_context)
    monkeypatch.setattr(pipeline, "set_span_ok", lambda _span: None)
    monkeypatch.setattr(pipeline, "set_span_error", lambda _span, _exc: None)
    monkeypatch.setattr(pipeline, "add_span_event_to", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "emit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "record_signal", lambda *_args, **_kwargs: None)
    return pipeline, metric_calls


@pytest.mark.asyncio
async def test_pipeline_step_metrics_success_and_error(
    patched_pipeline: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]],
) -> None:
    pipeline, metric_calls = patched_pipeline
    ctx = _ctx()

    async with pipeline._pipeline_step(ctx, "dedup") as (span, outcome):
        outcome["value"] = "custom"
        span.set_attribute("inside", True)

    with pytest.raises(RuntimeError):
        async with pipeline._pipeline_step(ctx, "dedup"):
            raise RuntimeError("boom")

    outcomes = [
        args[2]
        for name, args, _kwargs, action, _value in metric_calls
        if name == "WEBHOOK_PIPELINE_STEP_TOTAL" and action == "inc"
    ]
    assert "custom" in outcomes
    assert "error" in outcomes


@pytest.mark.asyncio
async def test_resolve_noise_context_reuse_and_fresh_analysis_paths(
    monkeypatch: pytest.MonkeyPatch,
    patched_pipeline: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]],
) -> None:
    pipeline, _metric_calls = patched_pipeline
    ctx = _ctx()

    async def reused(_key: str) -> DedupResult:
        return DedupResult(DedupAction.REUSE, {"importance": "high", "summary": "cached"}, 42, "redis_reuse")

    usage_calls: list[tuple[object, ...]] = []

    async def log_usage(*args: object, **kwargs: object) -> None:
        usage_calls.append((*args, kwargs))

    monkeypatch.setattr(pipeline, "resolve_dedup", reused)
    monkeypatch.setattr(pipeline, "log_ai_usage", log_usage)
    analysis, noise, dedup = await pipeline._resolve_noise_context(ctx, pipeline.WebhookPipelineDependencies())
    assert analysis["_route_type"] == "redis_reuse"
    assert noise.reason == "缓存复用路径"
    assert dedup.is_duplicate
    assert usage_calls[-1][0] == "redis_reuse"

    async def fresh(_key: str) -> DedupResult:
        return DedupResult(DedupAction.NEW, None, None)

    remembered: list[tuple[str, int, bool]] = []

    async def remember(key: str, *, original_event_id: int, **_kwargs: object) -> None:
        remembered.append((key, original_event_id, bool(_kwargs.get("reset_chain"))))

    async def analyze(_webhook_data: dict[str, object], **_kwargs: object) -> dict[str, object]:
        return {"importance": "medium", "summary": "fresh"}

    async def compute(*_args: object, **_kwargs: object) -> NoiseReductionContext:
        return NoiseReductionContext("standalone", None, 0.0, False, "none", 0, ())

    monkeypatch.setattr(pipeline, "resolve_dedup", fresh)
    monkeypatch.setattr(pipeline, "remember_dedup_state", remember)
    monkeypatch.setattr("services.analysis.ai_analyzer.analyze_webhook_with_ai", analyze)
    monkeypatch.setattr(pipeline, "compute_noise", compute)
    analysis, noise, dedup = await pipeline._resolve_noise_context(ctx, pipeline.WebhookPipelineDependencies())
    assert analysis["summary"] == "fresh"
    assert noise.relation == "standalone"
    assert dedup.action == DedupAction.NEW
    assert remembered == [(ctx.dedup_key, 0, False)]

    async def stale_new(_key: str) -> DedupResult:
        return DedupResult(DedupAction.NEW, None, None, reset_chain=True)

    remembered.clear()
    monkeypatch.setattr(pipeline, "resolve_dedup", stale_new)
    analysis, noise, dedup = await pipeline._resolve_noise_context(ctx, pipeline.WebhookPipelineDependencies())
    assert analysis["summary"] == "fresh"
    assert dedup.reset_chain is True
    assert remembered == [(ctx.dedup_key, 0, True)]


@pytest.mark.asyncio
async def test_run_processing_pipeline_suppressed_and_forward_decision_metrics(
    monkeypatch: pytest.MonkeyPatch,
    patched_pipeline: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]],
) -> None:
    pipeline, metric_calls = patched_pipeline
    ctx = _ctx()
    noise = NoiseReductionContext("standalone", None, 0.0, False, "none", 0, ())
    dedup_results = [
        DedupResult(DedupAction.NEW, None, None),
        DedupResult(DedupAction.NEW, None, None, reset_chain=True),
        DedupResult(DedupAction.RECHAIN, {"importance": "medium"}, 1, "rechain"),
    ]

    @asynccontextmanager
    async def suppressed_gate(_key: str):
        yield _GateResult(True, 9, "queue full")

    monkeypatch.setattr(pipeline, "alert_processing_gate", suppressed_gate)
    suppressed = await pipeline._run_processing_pipeline(ctx, pipeline.WebhookPipelineDependencies())
    assert suppressed.suppressed is True

    @asynccontextmanager
    async def open_gate(_key: str):
        yield _GateResult(False)

    async def resolved(
        *_args: object, **_kwargs: object
    ) -> tuple[dict[str, object], NoiseReductionContext, DedupResult]:
        return {"importance": "medium", "summary": "ok"}, noise, dedup_results.pop(0)

    remembered: list[bool] = []
    scheduled: list[list[int]] = []

    async def remember(*_args: object, **_kwargs: object) -> None:
        remembered.append(bool(_kwargs.get("reset_chain")))

    async def schedule(ids: list[int]) -> None:
        scheduled.append(ids)

    finalize_results = [
        FinalizeAnalysisResult(SaveWebhookResult(1, False, None), None, []),
        FinalizeAnalysisResult(
            SaveWebhookResult(2, False, None),
            ForwardDecision(False, "冷却窗口中", False, []),
            [],
        ),
        FinalizeAnalysisResult(
            SaveWebhookResult(3, False, None),
            ForwardDecision(True, None, False, [_rule("feishu")]),
            [10, 11],
        ),
    ]

    async def finalize(*_args: object, **_kwargs: object) -> FinalizeAnalysisResult:
        return finalize_results.pop(0)

    monkeypatch.setattr(pipeline, "alert_processing_gate", open_gate)
    monkeypatch.setattr(pipeline, "_resolve_noise_context", resolved)
    monkeypatch.setattr(pipeline, "remember_dedup_state", remember)
    monkeypatch.setattr(pipeline, "schedule_forward_outbox_many", schedule)
    monkeypatch.setattr(pipeline, "finalize_analysis_transaction", finalize)

    unknown = await pipeline._run_processing_pipeline(ctx, pipeline.WebhookPipelineDependencies())
    skipped = await pipeline._run_processing_pipeline(ctx, pipeline.WebhookPipelineDependencies())
    queued = await pipeline._run_processing_pipeline(ctx, pipeline.WebhookPipelineDependencies())

    assert unknown.save_result.webhook_id == 1
    assert skipped.forward_decision.should_forward is False
    assert queued.outbox_count == 2
    assert scheduled[-1] == [10, 11]
    assert remembered == [False, True, True]


def _rule(target_type: str = "webhook") -> ForwardRuleSnapshot:
    return ForwardRuleSnapshot(
        id=1,
        name="rule",
        match_event_type="",
        match_importance="",
        match_source="",
        match_duplicate="",
        match_payload="",
        target_type=target_type,
        target_url="https://target.test/hook",
        stop_on_match=True,
        target_name="target",
    )


def test_log_completed_processing_branches(
    patched_pipeline: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]],
) -> None:
    pipeline, _metric_calls = patched_pipeline
    ctx = _ctx()
    span = _Span()
    noise = NoiseReductionContext("derived", 2, 0.8, True, "same root", 1, (2,))

    with pytest.raises(RuntimeError, match="missing final state"):
        pipeline._log_completed_processing(
            ctx=ctx,
            result=pipeline.PipelineProcessingResult(False),
            request_id="req",
            start_perf=0.0,
            span=None,
        )

    for decision, outbox_count in (
        (None, 0),
        (ForwardDecision(False, "未匹配规则", False, []), 0),
        (ForwardDecision(True, None, False, [_rule()]), 0),
        (ForwardDecision(True, None, True, [_rule()]), 2),
    ):
        pipeline._log_completed_processing(
            ctx=ctx,
            result=pipeline.PipelineProcessingResult(
                False,
                save_result=SaveWebhookResult(100, decision is None, 42),
                forward_decision=decision,
                noise=noise,
                final_analysis={"importance": "high", "summary": "ok", "_route_type": "ai"},
                outbox_count=outbox_count,
            ),
            request_id="req",
            start_perf=0.0,
            span=span,
        )

    assert span.attrs["webhook.importance"] == "high"
    assert span.attrs["webhook.route"] == "ai"


@pytest.mark.asyncio
async def test_handle_webhook_ingest_and_raw_ingest_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    patched_pipeline: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]],
) -> None:
    pipeline, metric_calls = patched_pipeline

    envelopes: list[object] = []
    real_raw_ingest = pipeline._handle_raw_ingest

    async def raw_handler(envelope: object, _client_ip: str, **_kwargs: object) -> None:
        envelopes.append(envelope)

    monkeypatch.setattr(pipeline, "_handle_raw_ingest", raw_handler)
    monkeypatch.setattr(pipeline, "set_fallback_trace_id", lambda _trace_id: "token")
    monkeypatch.setattr(pipeline, "reset_fallback_trace_id", lambda _token: None)
    monkeypatch.setattr(pipeline, "get_current_trace_id", lambda: "")
    await pipeline.handle_webhook_ingest(
        source="prometheus",
        raw_headers={},
        raw_body="[not-object]",
        client_ip="127.0.0.1",
        request_id="req",
    )
    assert envelopes[0].payload is None
    monkeypatch.setattr(pipeline, "_handle_raw_ingest", real_raw_ingest)

    req_ctx = _ctx().req_ctx

    def parse_ok(*_args: object, **_kwargs: object) -> WebhookRequestContext:
        return req_ctx

    async def run_suppressed(*_args: object, **_kwargs: object) -> Any:
        return pipeline.PipelineProcessingResult(True)

    monkeypatch.setattr(pipeline, "parse_request", parse_ok)
    monkeypatch.setattr(pipeline, "generate_event_keys", lambda *_args: ("hash", "dedup"))
    monkeypatch.setattr(pipeline, "_run_processing_pipeline", run_suppressed)
    await pipeline._handle_raw_ingest(
        pipeline.EventEnvelope({}, {"ok": True}, b"{}", "prometheus", "ts", "req"),
        dependencies=pipeline.WebhookPipelineDependencies(),
    )

    async def run_completed(*_args: object, **_kwargs: object) -> Any:
        return pipeline.PipelineProcessingResult(
            False,
            save_result=SaveWebhookResult(1, False, None),
            forward_decision=None,
            noise=NoiseReductionContext("standalone", None, 0.0, False, "none", 0, ()),
            final_analysis={"importance": "medium", "summary": "ok"},
        )

    monkeypatch.setattr(pipeline, "_run_processing_pipeline", run_completed)
    await pipeline._handle_raw_ingest(
        pipeline.EventEnvelope({}, {"ok": True}, b"{}", "prometheus", "ts", "req"),
        dependencies=pipeline.WebhookPipelineDependencies(),
    )

    def parse_fail(*_args: object, **_kwargs: object) -> WebhookRequestContext:
        raise ValueError("bad payload")

    monkeypatch.setattr(pipeline, "parse_request", parse_fail)
    with pytest.raises(ValueError, match="bad payload"):
        await pipeline._handle_raw_ingest(
            pipeline.EventEnvelope({}, {"ok": True}, b"{}", "prometheus", "ts", "req"),
            dependencies=pipeline.WebhookPipelineDependencies(),
        )

    outcomes = [
        kwargs["outcome"]
        for name, _args, kwargs, action, _value in metric_calls
        if name == "WEBHOOK_PROCESSING_DURATION_SECONDS" and action == "observe"
    ]
    assert {"suppressed", "completed", "failed"} <= set(outcomes)
