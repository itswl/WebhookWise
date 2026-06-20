"""Tests for the decision-trace assembly (services.webhooks.decision_trace).

The builders are pure — no DB, no async — so they exercise every forward/skip
shape directly. The persist helper is covered by an async test that asserts the
SAVEPOINT-isolated, non-blocking degradation contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.dedup import DedupAction, DedupResult
from services.webhooks.decision_trace import (
    build_decision_trace,
    build_trace_steps,
    record_decision_trace,
)
from services.webhooks.decisioning import ForwardDecision, ForwardRuleSnapshot
from services.webhooks.types import NoiseReductionContext

# ── helpers ──────────────────────────────────────────────────────────


def _dedup(action: DedupAction = DedupAction.NEW, original_event_id: int | None = None) -> DedupResult:
    return DedupResult(action=action, analysis=None, original_event_id=original_event_id)


def _noise(suppress: bool = False, relation: str = "standalone", reason: str = "") -> NoiseReductionContext:
    return NoiseReductionContext(
        relation=relation,
        root_cause_event_id=None,
        confidence=0.0,
        suppress_forward=suppress,
        reason=reason,
        related_alert_count=0,
        related_alert_ids=(),
    )


def _rule(name: str) -> ForwardRuleSnapshot:
    return ForwardRuleSnapshot(
        id=1,
        name=name,
        match_event_type="",
        match_importance="",
        match_source="",
        match_duplicate="all",
        match_payload="",
        target_type="webhook",
        target_url="http://example.com/hook",
        stop_on_match=False,
    )


def _analysis(importance: str = "high", route: str = "ai") -> dict[str, Any]:
    return {"importance": importance, "summary": "s", "_route_type": route}


def _step_named(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(s for s in steps if s["step"] == name)


# ── build_trace_steps ────────────────────────────────────────────────


def test_steps_forwarded_chain_is_ordered_and_complete() -> None:
    decision = ForwardDecision(True, None, False, matched_rules=[_rule("feishu")])
    steps = build_trace_steps(
        dedup=_dedup(),
        final_analysis=_analysis(),  # type: ignore[arg-type]
        noise=_noise(),
        decision=decision,
    )
    assert [s["step"] for s in steps] == ["dedup", "analysis", "noise", "rule_match", "forward"]
    assert _step_named(steps, "rule_match")["matched"] == ["feishu"]
    fwd = _step_named(steps, "forward")
    assert fwd["outcome"] == "forwarded"
    assert fwd["skip_code"] == "none"


def test_steps_no_match_skip() -> None:
    decision = ForwardDecision(False, "No matching forwarding rule", False, skip_code="no_match")
    steps = build_trace_steps(
        dedup=_dedup(), final_analysis=_analysis(), noise=_noise(), decision=decision  # type: ignore[arg-type]
    )
    fwd = _step_named(steps, "forward")
    assert fwd["outcome"] == "skipped"
    assert fwd["skip_code"] == "no_match"
    # No silence step when the alert was not silenced.
    assert all(s["step"] != "silence" for s in steps)


def test_steps_silenced_includes_silence_step_with_id() -> None:
    decision = ForwardDecision(
        False, "Silenced (id=7)", False, skip_code="silenced", silence_id=7
    )
    steps = build_trace_steps(
        dedup=_dedup(), final_analysis=_analysis(route="silenced_skip"), noise=_noise(), decision=decision  # type: ignore[arg-type]
    )
    silence = _step_named(steps, "silence")
    assert silence["matched"] is True
    assert silence["silence_id"] == 7
    assert _step_named(steps, "analysis")["route"] == "silenced_skip"


def test_steps_noise_suppressed_reflected_in_noise_step() -> None:
    decision = ForwardDecision(False, "Smart noise reduction suppressed forwarding: x", False, skip_code="noise_suppressed")
    steps = build_trace_steps(
        dedup=_dedup(),
        final_analysis=_analysis(),  # type: ignore[arg-type]
        noise=_noise(suppress=True, relation="child", reason="x"),
        decision=decision,
    )
    noise = _step_named(steps, "noise")
    assert noise["suppress_forward"] is True
    assert noise["relation"] == "child"


def test_steps_duplicate_cooldown() -> None:
    decision = ForwardDecision(False, "Just notified, in cooldown", False, skip_code="cooldown")
    steps = build_trace_steps(
        dedup=_dedup(action=DedupAction.REUSE, original_event_id=42),
        final_analysis=_analysis(route="redis_reuse"),  # type: ignore[arg-type]
        noise=_noise(),
        decision=decision,
    )
    dedup_step = _step_named(steps, "dedup")
    assert dedup_step["is_duplicate"] is True
    assert dedup_step["original_event_id"] == 42
    assert _step_named(steps, "forward")["skip_code"] == "cooldown"


def test_steps_periodic_reminder_flag() -> None:
    decision = ForwardDecision(True, None, True, matched_rules=[_rule("feishu")])
    steps = build_trace_steps(
        dedup=_dedup(action=DedupAction.REUSE, original_event_id=9),
        final_analysis=_analysis(),  # type: ignore[arg-type]
        noise=_noise(),
        decision=decision,
    )
    assert _step_named(steps, "forward")["is_periodic_reminder"] is True


# ── build_decision_trace (flattened row) ─────────────────────────────


def test_build_row_flattens_outcome_and_indexed_fields() -> None:
    decision = ForwardDecision(True, None, False, matched_rules=[_rule("feishu"), _rule("openclaw")])
    trace = build_decision_trace(
        webhook_event_id=123,
        source="grafana",
        dedup=_dedup(),
        final_analysis=_analysis(importance="P0"),  # type: ignore[arg-type]
        noise=_noise(),
        decision=decision,
    )
    assert trace.webhook_event_id == 123
    assert trace.outcome == "forwarded"
    assert trace.skip_code == "none"
    assert trace.source == "grafana"
    assert trace.importance == "p0"  # normalized
    assert trace.is_periodic_reminder is False
    assert trace.matched_rules == ["feishu", "openclaw"]
    assert trace.steps is not None


def test_build_row_skipped_outcome() -> None:
    decision = ForwardDecision(False, "No matching forwarding rule", False, skip_code="no_match")
    trace = build_decision_trace(
        webhook_event_id=1,
        source="",
        dedup=_dedup(),
        final_analysis={"importance": "", "summary": ""},
        noise=_noise(),
        decision=decision,
    )
    assert trace.outcome == "skipped"
    assert trace.skip_code == "no_match"
    # Empty source/importance flatten to NULL rather than empty string.
    assert trace.source is None
    assert trace.importance is None


# ── AI judgment quality signals (Phase B) ────────────────────────────


def test_steps_and_row_capture_importance_override() -> None:
    analysis = {
        "importance": "high",
        "summary": "s",
        "_route_type": "ai",
        "_importance_override": "gpu_high",
        "_importance_override_reason": "GPU saturated",
    }
    decision = ForwardDecision(True, None, False, matched_rules=[_rule("feishu")])
    steps = build_trace_steps(
        dedup=_dedup(), final_analysis=analysis, noise=_noise(), decision=decision  # type: ignore[arg-type]
    )
    analysis_step = _step_named(steps, "analysis")
    assert analysis_step["importance_override"] is True
    assert analysis_step["importance_override_reason"] == "GPU saturated"

    trace = build_decision_trace(
        webhook_event_id=1, source="volcengine", dedup=_dedup(),
        final_analysis=analysis, noise=_noise(), decision=decision,  # type: ignore[arg-type]
    )
    assert trace.route == "ai"
    assert trace.importance_override is True
    assert trace.degraded_reason is None


def test_steps_and_row_capture_degradation_reason() -> None:
    analysis = {
        "importance": "medium",
        "summary": "s",
        "_route_type": "rule",
        "_degraded": True,
        "_degraded_reason": "ai_error: boom",
    }
    decision = ForwardDecision(False, "No matching forwarding rule", False, skip_code="no_match")
    steps = build_trace_steps(
        dedup=_dedup(), final_analysis=analysis, noise=_noise(), decision=decision  # type: ignore[arg-type]
    )
    analysis_step = _step_named(steps, "analysis")
    assert analysis_step["degraded"] is True
    assert analysis_step["degraded_reason"] == "ai_error: boom"

    trace = build_decision_trace(
        webhook_event_id=2, source="grafana", dedup=_dedup(),
        final_analysis=analysis, noise=_noise(), decision=decision,  # type: ignore[arg-type]
    )
    assert trace.route == "rule"
    assert trace.importance_override is False
    assert trace.degraded_reason == "ai_error: boom"


def test_row_truncates_long_degraded_reason() -> None:
    analysis = {
        "importance": "low",
        "summary": "s",
        "_route_type": "rule",
        "_degraded": True,
        "_degraded_reason": "x" * 500,
    }
    decision = ForwardDecision(False, "x", False, skip_code="no_match")
    trace = build_decision_trace(
        webhook_event_id=3, source="s", dedup=_dedup(),
        final_analysis=analysis, noise=_noise(), decision=decision,  # type: ignore[arg-type]
    )
    assert trace.degraded_reason is not None
    assert len(trace.degraded_reason) == 200


# ── record_decision_trace (persist contract) ─────────────────────────


@pytest.mark.asyncio
async def test_record_uses_savepoint_and_adds_trace() -> None:
    session = MagicMock()
    nested_cm = AsyncMock()
    session.begin_nested.return_value = nested_cm
    decision = ForwardDecision(True, None, False, matched_rules=[_rule("feishu")])

    await record_decision_trace(
        session,
        webhook_event_id=5,
        source="grafana",
        dedup=_dedup(),
        final_analysis=_analysis(),  # type: ignore[arg-type]
        noise=_noise(),
        decision=decision,
    )

    session.begin_nested.assert_called_once()
    session.add.assert_called_once()
    added = session.add.call_args.args[0]
    assert added.webhook_event_id == 5
    assert added.outcome == "forwarded"


@pytest.mark.asyncio
async def test_record_swallows_errors_and_never_raises() -> None:
    # A trace-write failure must never propagate into the forward decision —
    # even an unexpected error type is degraded to a warning.
    session = MagicMock()
    session.begin_nested.side_effect = RuntimeError("savepoint exploded")
    decision = ForwardDecision(False, "x", False, skip_code="no_match")

    await record_decision_trace(
        session,
        webhook_event_id=5,
        source="grafana",
        dedup=_dedup(),
        final_analysis=_analysis(),  # type: ignore[arg-type]
        noise=_noise(),
        decision=decision,
    )

    # The error was swallowed; no trace was added.
    session.add.assert_not_called()
