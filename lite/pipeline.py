"""The suppression chain — the part that decides whether to interrupt a human.

Every gate can only do two things: pass, or stop with a NAMED reason. That name
(`skip_code`) is written to the decision trace alongside the ordered chain of
steps, so "why didn't I get notified?" is a query, not an investigation.

Gate order is deliberate — cheapest and most certain first:

    ① duplicate  — identical alert already seen inside the window
    ② silenced   — an operator asked for quiet
    ③ cooldown   — we just notified about this identity
    ④ no_match   — no forwarding rule claims this alert

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
) -> dict[str, Any]:
    """Run one alert through the chain. Always produces exactly one decision."""
    decision = Decision()
    event = normalize(source, payload)
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

    # ③ Cooldown — we notified about this identity very recently.
    if settings.cooldown_seconds > 0:
        last = await store.last_forward_at(event["alert_hash"])
        if last is not None and (time.time() - last) < settings.cooldown_seconds:
            decision.record("cooldown", "cooling", seconds_ago=int(time.time() - last))
            await store.insert_decision(event_id, "skipped", "cooldown", decision.steps, [])
            return {"event_id": event_id, "outcome": "skipped", "skip_code": "cooldown"}
    decision.record("cooldown", "pass", seconds=settings.cooldown_seconds)

    # ④ Rule match — an alert nobody routed is an alert nobody asked for.
    matched = [rule for rule in await store.active_rules() if _matches(rule, event)]
    if not matched:
        decision.record("rules", "no_match")
        await store.insert_decision(event_id, "skipped", "no_match", decision.steps, [])
        return {"event_id": event_id, "outcome": "skipped", "skip_code": "no_match"}

    for rule in matched:
        await store.enqueue_delivery(
            event_id,
            str(rule["name"]),
            str(rule["target_kind"]),
            str(rule["target_url"]),
            channels.build_payload(str(rule["target_kind"]), event),
        )
    names = [str(rule["name"]) for rule in matched]
    decision.record("rules", "matched", rules=names)
    decision.record("forward", "enqueued", count=len(matched))
    await store.insert_decision(event_id, "forwarded", "none", decision.steps, names)
    return {"event_id": event_id, "outcome": "forwarded", "skip_code": "none", "rules": names}


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
