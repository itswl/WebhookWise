"""Assemble and persist a per-alert decision trace.

A decision trace is the queryable answer to "why was this alert forwarded or
skipped". It records the ordered chain of pipeline decisions (dedup → analysis →
noise → silence → rule match → forward) as JSONB, alongside a flattened,
indexed outcome/skip_code so the dashboard can both aggregate ("how many were
silenced / cooled-down / forwarded over the last day") and show the full
per-alert reasoning.

The trace is written in the same transaction as the event persist, inside a
SAVEPOINT so a trace-write failure rolls back only the trace and never the
authoritative forward decision. Assembly is a pure function so it can be tested
without a database.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from models import DecisionTrace
from services.dedup import DedupResult
from services.webhooks.decisioning import ForwardDecision, normalize_importance
from services.webhooks.types import (
    AnalysisResult,
    NoiseReductionContext,
    analysis_degraded_reason,
    analysis_route,
    is_analysis_degraded,
)

# Literal key set by services.analysis.resource_risk when a deterministic rule
# overrides (promotes) the AI's importance. Its presence is the signal that the
# system disagreed with the AI's judgment.
_IMPORTANCE_OVERRIDE_KEY = "_importance_override"


def _has_importance_override(final_analysis: AnalysisResult) -> bool:
    return bool(final_analysis.get(_IMPORTANCE_OVERRIDE_KEY))


def _degraded_reason_column(final_analysis: AnalysisResult) -> str | None:
    """Degradation reason trimmed to the column width (NULL when not degraded)."""
    reason = analysis_degraded_reason(final_analysis)
    return reason[:200] if reason else None

logger = get_logger("webhooks.decision_trace")


def build_trace_steps(
    *,
    dedup: DedupResult,
    final_analysis: AnalysisResult,
    noise: NoiseReductionContext,
    decision: ForwardDecision,
) -> list[dict[str, Any]]:
    """Build the ordered decision chain for one processed alert.

    Steps follow the order the pipeline actually evaluates them so the trace
    reads top-to-bottom as a narrative: dedup → analysis → noise → silence →
    rule match → forward. The final ``forward`` step carries the authoritative
    outcome and skip_code (whichever suppressor or rule won).
    """
    steps: list[dict[str, Any]] = [
        {
            "step": "dedup",
            "action": dedup.action.value,
            "is_duplicate": dedup.is_duplicate,
            "original_event_id": dedup.original_event_id,
        },
        {
            "step": "analysis",
            "route": analysis_route(final_analysis, default="ai"),
            "importance": normalize_importance(final_analysis.get("importance", "")),
            "degraded": is_analysis_degraded(final_analysis),
            "degraded_reason": analysis_degraded_reason(final_analysis) or None,
            "importance_override": _has_importance_override(final_analysis),
            "importance_override_reason": final_analysis.get("_importance_override_reason") or None,
        },
        {
            "step": "noise",
            "relation": noise.relation,
            "suppress_forward": noise.suppress_forward,
            "reason": noise.reason,
            "related_alert_count": noise.related_alert_count,
        },
    ]

    if decision.skip_code == "silenced":
        steps.append({"step": "silence", "matched": True, "silence_id": decision.silence_id})

    steps.append({"step": "rule_match", "matched": [rule.name for rule in decision.matched_rules]})
    steps.append(
        {
            "step": "forward",
            "outcome": "forwarded" if decision.should_forward else "skipped",
            "skip_code": decision.skip_code,
            "skip_reason": decision.skip_reason,
            "is_periodic_reminder": decision.is_periodic_reminder,
        }
    )
    return steps


def build_decision_trace(
    *,
    webhook_event_id: int,
    source: str,
    dedup: DedupResult,
    final_analysis: AnalysisResult,
    noise: NoiseReductionContext,
    decision: ForwardDecision,
) -> DecisionTrace:
    """Build (but do not persist) the DecisionTrace row for one processed alert."""
    return DecisionTrace(
        webhook_event_id=webhook_event_id,
        outcome="forwarded" if decision.should_forward else "skipped",
        skip_code=decision.skip_code,
        source=source or None,
        importance=normalize_importance(final_analysis.get("importance", "")) or None,
        is_periodic_reminder=decision.is_periodic_reminder,
        route=analysis_route(final_analysis, default="ai"),
        importance_override=_has_importance_override(final_analysis),
        degraded_reason=_degraded_reason_column(final_analysis),
        # Set only when this alert was silenced, so the per-rule ROI aggregate
        # (which silence suppressed how many) can GROUP BY an indexed column.
        silence_id=decision.silence_id if decision.skip_code == "silenced" else None,
        matched_rules=[rule.name for rule in decision.matched_rules],
        steps=build_trace_steps(
            dedup=dedup, final_analysis=final_analysis, noise=noise, decision=decision
        ),
    )


async def record_decision_trace(
    session: AsyncSession,
    *,
    webhook_event_id: int,
    source: str,
    dedup: DedupResult,
    final_analysis: AnalysisResult,
    noise: NoiseReductionContext,
    decision: ForwardDecision,
) -> None:
    """Persist a decision trace inside the caller's transaction, non-blocking.

    Runs in a SAVEPOINT so a trace-write failure rolls back only the trace,
    leaving the surrounding event-persist transaction (and the authoritative
    forward decision) intact. Any error is swallowed with a warning — the trace
    is an observability record, never a gate on the pipeline.
    """
    try:
        trace = build_decision_trace(
            webhook_event_id=webhook_event_id,
            source=source,
            dedup=dedup,
            final_analysis=final_analysis,
            noise=noise,
            decision=decision,
        )
        async with session.begin_nested():
            session.add(trace)
    except Exception as exc:  # noqa: BLE001 - a trace is observability, never a gate on the pipeline
        logger.warning("[DecisionTrace] Failed to record trace for event_id=%s: %s", webhook_event_id, exc)
