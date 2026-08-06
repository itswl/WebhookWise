"""The suppression chain — the part that decides whether to interrupt a human.

Every gate can only do two things: pass, or stop with a NAMED reason. That name
(`skip_code`) is written to the decision trace alongside the ordered chain of
steps, so "why didn't I get notified?" is a query, not an investigation.

Gate order is deliberate — cheapest and most certain first:

    ① duplicate  — identical alert already seen inside the window
    ② silenced   — an operator asked for quiet
    ③ no_match   — no forwarding rule claims this alert
    ④ cooldown   — every rule that claims it was notified too recently

Cooldown comes last because it is scoped per (identity, rule): which
destinations a repeat alert should still reach cannot be decided before
knowing which rules claim it. A partially-cooled alert is still delivered —
to the rules that are due — and the trace names the ones held back.

Anything that survives all four is delivered, and that too is recorded.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from lite import channels, triage
from lite.normalize import normalize
from lite.store import Store


class Decision:
    """Accumulates the ordered gate chain while the alert walks the pipeline."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def record(self, step: str, result: str, **detail: Any) -> None:
        self.steps.append({"step": step, "result": result, **detail})


async def process(
    store: Store,
    client: httpx.AsyncClient,
    settings: Any,
    source: str,
    payload: Any,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Run one alert through the chain. Always produces exactly one decision."""
    decision = Decision()
    event = normalize(source, payload)
    # Quoted back on the way out (see channels.build_payload): a relay fanning
    # this same alert to several brains can then gather what each one made of
    # it under the original.
    if correlation_id:
        event["correlation_id"] = correlation_id
        decision.record("correlate", "quoted", correlation_id=correlation_id)
    decision.record(
        "normalize",
        "ok",
        title=event["title"],
        alert_hash=event["alert_hash"],
        resolved=event["resolved"],
    )

    # ① Duplicate — the same identity inside the dedup window.
    duplicate = await store.recent_duplicate(event["alert_hash"], settings.dedup_window_seconds)
    if duplicate is not None:
        age = int(time.time() - float(duplicate["received_at"]))
        decision.record("dedup", "duplicate", first_event_id=duplicate["id"], seconds_ago=age)
        event_id = await _persist(store, event, {"importance": None, "summary": None, "route": "skipped"})
        await store.insert_decision(event_id, "skipped", "duplicate", decision.steps, [])
        return {"event_id": event_id, "outcome": "skipped", "skip_code": "duplicate"}
    decision.record("dedup", "pass", window_seconds=settings.dedup_window_seconds)

    # ② Silence — checked before analysis so a silenced alert costs no AI call.
    haystack = f"{event['source']} {event['title']} {event['body']}".lower()
    for silence in await store.active_silences():
        if str(silence["pattern"]).lower() in haystack:
            decision.record("silence", "silenced", silence_id=silence["id"], pattern=silence["pattern"])
            event_id = await _persist(store, event, {"importance": None, "summary": None, "route": "skipped"})
            await store.insert_decision(event_id, "skipped", "silenced", decision.steps, [])
            return {"event_id": event_id, "outcome": "skipped", "skip_code": "silenced"}
    decision.record("silence", "pass")

    # Analysis runs only for alerts that might actually be delivered.
    analysis = await triage.triage(client, settings, source, event["title"], event["body"], event["resolved"])
    decision.record(
        "analysis",
        analysis["route"],
        importance=analysis["importance"],
        degraded=analysis.get("degraded"),
    )
    event_id = await _persist(store, event, analysis)
    event.update({"importance": analysis["importance"], "summary": analysis["summary"], "route": analysis["route"]})

    # ③ Rule match — an alert nobody routed is an alert nobody asked for.
    matched, stopped_by = _match_rules(await store.active_rules(), event)
    if not matched:
        decision.record("rules", "no_match")
        await store.insert_decision(event_id, "skipped", "no_match", decision.steps, [])
        return {"event_id": event_id, "outcome": "skipped", "skip_code": "no_match"}
    decision.record("rules", "matched", rules=[str(r["name"]) for r in matched], stopped_by=stopped_by)

    # ④ Cooldown — per (identity, rule), so each destination paces itself.
    # Runs AFTER matching for exactly that reason: which rules a repeat alert
    # should still reach cannot be decided before knowing which rules claim it.
    due, cooled = await _apply_cooldown(store, settings, event["alert_hash"], matched)
    if not due:
        decision.record("cooldown", "cooling", cooled=cooled)
        await store.insert_decision(event_id, "skipped", "cooldown", decision.steps, [])
        return {"event_id": event_id, "outcome": "skipped", "skip_code": "cooldown"}
    decision.record("cooldown", "pass", seconds=settings.cooldown_seconds, cooled=cooled or None)

    for rule in due:
        await store.enqueue_delivery(
            event_id,
            str(rule["name"]),
            str(rule["target_kind"]),
            str(rule["target_url"]),
            channels.build_payload(str(rule["target_kind"]), event),
        )
    names = [str(rule["name"]) for rule in due]
    decision.record("forward", "enqueued", count=len(due))
    await store.insert_decision(event_id, "forwarded", "none", decision.steps, names)
    return {"event_id": event_id, "outcome": "forwarded", "skip_code": "none", "rules": names}


async def _apply_cooldown(
    store: Store, settings: Any, alert_hash: str, matched: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split matched rules into those due for delivery and those still cooling."""
    if settings.cooldown_seconds <= 0:
        return matched, []
    now = time.time()
    due: list[dict[str, Any]] = []
    cooled: list[str] = []
    for rule in matched:
        last = await store.last_forward_at(alert_hash, str(rule["name"]))
        if last is not None and (now - last) < settings.cooldown_seconds:
            cooled.append(str(rule["name"]))
        else:
            due.append(rule)
    return due, cooled


def _match_rules(rules: list[dict[str, Any]], event: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Rules in priority order; a matched stop_on_match rule ends evaluation.

    Returns the matched rules and the name of the rule that stopped the scan
    (None if every rule was considered). Same semantics as the full edition, so
    a routing table can be moved between them unchanged.
    """
    matched: list[dict[str, Any]] = []
    for rule in rules:
        if not _matches(rule, event):
            continue
        matched.append(rule)
        if rule.get("stop_on_match"):
            return matched, str(rule["name"])
    return matched, None


def _matches(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    def csv_ok(raw: Any, value: Any) -> bool:
        wanted = [part.strip().lower() for part in str(raw or "").split(",") if part.strip()]
        return not wanted or str(value or "").lower() in wanted

    return csv_ok(rule.get("match_source"), event.get("source")) and csv_ok(
        rule.get("match_importance"), event.get("importance")
    )


async def _persist(store: Store, event: dict[str, Any], analysis: dict[str, Any]) -> int:
    return await store.insert_event(
        {
            "received_at": time.time(),
            "source": event["source"],
            "title": event["title"],
            "body": event["body"],
            "alert_hash": event["alert_hash"],
            "importance": analysis.get("importance"),
            "summary": analysis.get("summary"),
            "route": analysis.get("route"),
            "raw": event["raw"],
        }
    )
