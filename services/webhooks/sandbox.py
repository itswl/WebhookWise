"""Dry-run a webhook payload through the pre-AI pipeline, with zero side effects.

Powers the "test sandbox": an operator pastes a raw payload + picks a source,
and we report what WebhookWise WOULD extract and decide — which adapter parsed
it, the alert identity/hash, the deterministic (rule-based) importance, and
which forward rules / silences would match — WITHOUT enqueuing, calling the AI,
or persisting anything.

Every step here is a pure function except loading the live forward rules and
silences (two cached, read-only DB queries the caller passes a session for).
Deliberately omitted because they need real state the dry-run cannot have:
  - dedup "is this a duplicate" (needs a Redis/DB lookup) — we report
    is_duplicate=False and say so.
  - the AI-refined importance/summary/root-cause (needs a real model call) — we
    report the deterministic rule-based judgment and label it as such.

Two boundaries that must mirror production to stay faithful:
  - forward-rule ``match_event_type`` is tested against the constant
    ``"webhook_forward"`` at the forward stage, NOT the payload's event type
    (see services/webhooks/forwarding_stage.py), so we match with that constant.
  - ``is_duplicate=False``, so the cooldown / periodic-reminder branches of
    ``decide_forwarding`` stay inert and the verdict is the fresh-alert one.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from adapters.ecosystem_adapters import normalize_webhook_event
from services.analysis.ai_analyzer import analyze_with_rules
from services.analysis.alert_identity_context import build_alert_identity_context
from services.dedup import generate_event_keys
from services.forwarding.rules import get_cached_forward_rules
from services.silences.store import get_cached_active_silences
from services.webhooks.decisioning import (
    decide_forwarding,
    extract_forward_match_fields,
    forwarding_policy_from_config,
    normalize_importance,
)

# The forward stage routes every forwarded alert under this constant event type,
# so forward-rule match_event_type is tested against it, not the payload's type.
_FORWARD_EVENT_TYPE = "webhook_forward"


def _rule_summary(rule: Any) -> dict[str, Any]:
    """A non-secret summary of a matched forward rule (never the target URL)."""
    return {
        "id": rule.id,
        "name": rule.name,
        "target_type": rule.target_type,
        "target_name": rule.target_name or None,
        "stop_on_match": rule.stop_on_match,
    }


async def test_webhook_payload(
    session: AsyncSession, *, source: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Dry-run a pasted payload through parse → identity → rules/silence match.

    Returns a structured "what WW would do" report. No enqueue, no AI call, no
    persistence; the only I/O is loading the live forward rules and silences.
    """
    # 1. Parse / normalize exactly as ingestion does (pure adapter dispatch).
    normalized = normalize_webhook_event(payload, source)
    parsed = dict(normalized.data)
    resolved_source = normalized.source

    # 2. Fingerprints (pure sha256; no Redis).
    alert_hash, dedup_key = generate_event_keys(parsed, resolved_source)

    # 3. Extracted identity for display (the same context the AI prompt uses).
    identity = build_alert_identity_context(resolved_source, parsed)
    match_fields = extract_forward_match_fields(parsed)

    # 4. Deterministic, rule-based judgment — what WW uses when the AI is
    #    unavailable or tiered-routing skips the model. NOT the AI's verdict.
    rule_analysis = analyze_with_rules(parsed, resolved_source)
    importance = normalize_importance(rule_analysis.get("importance", "")) or "unknown"
    event_type = str(rule_analysis.get("event_type", "") or "")

    # 5. Live config — the only I/O, both read-only and cached.
    rules = await get_cached_forward_rules(session=session)
    silences = await get_cached_active_silences(session=session)

    # 6. The forward verdict, mirroring production (constant event type,
    #    is_duplicate=False so cooldown/reminder branches stay inert).
    decision = decide_forwarding(
        event_type=_FORWARD_EVENT_TYPE,
        importance=importance,
        is_duplicate=False,
        source=resolved_source,
        rules=rules,
        policy=forwarding_policy_from_config(),
        parsed_data=parsed,
        silences=silences,
    )

    silenced_by = (
        {"silence_id": decision.silence_id} if decision.skip_code == "silenced" else None
    )

    return {
        "source": {
            "input": source,
            "resolved": resolved_source,
            "adapter": normalized.adapter,
            "matched": normalized.adapter != "passthrough",
        },
        "alert_hash": alert_hash,
        "dedup_key": dedup_key,
        "identity": identity.get("identity", {}),
        "resources": identity.get("resources", []),
        "metrics": identity.get("metrics", []),
        "match_fields": match_fields,
        # Clearly flagged as the deterministic fallback, not the AI's judgment.
        "rule_based_analysis": {
            "importance": importance,
            "event_type": event_type,
            "summary": rule_analysis.get("summary") or None,
            "note": "Rule-based fallback judgment (no AI call). The AI may refine this.",
        },
        "forwarding": {
            "should_forward": decision.should_forward,
            "skip_code": decision.skip_code,
            "skip_reason": decision.skip_reason,
            "matched_rules": [_rule_summary(r) for r in decision.matched_rules],
            "silenced_by": silenced_by,
        },
        # The dry-run cannot know real dedup state without hitting Redis/DB.
        "dedup_note": "Duplicate status is not evaluated in a dry-run (assumed new).",
    }
