"""Outbound delivery: build the message, then send it.

Building and sending are separate so the payload can be persisted in the outbox
(and inspected in the dashboard) before any network call happens.
"""

from __future__ import annotations

from typing import Any

import httpx

_COLOR = {"high": "red", "medium": "orange", "low": "grey"}


def build_payload(kind: str, event: dict[str, Any]) -> dict[str, Any]:
    if kind == "feishu":
        return _feishu_card(event)
    # Shaped as {meta: ...} so a relay door can read identity with one template
    # and gather this reading beside other brains' readings of the same alert.
    return {
        "source": event["source"],
        "title": event["title"],
        "status": "resolved" if event.get("resolved") else "firing",
        "importance": event["importance"],
        "summary": event["summary"],
        "body": event["body"],
        "route": event["route"],
        "meta": {
            "brain": "ww-lite",
            "alert_name": event["title"],
            "source": event["source"],
            "importance": event["importance"],
            "summary": event["summary"],
            "route": event["route"],
            "correlation_id": event.get("correlation_id", ""),
        },
    }


def _feishu_card(event: dict[str, Any]) -> dict[str, Any]:
    importance = str(event.get("importance") or "medium")
    resolved = bool(event.get("resolved"))
    lines = [
        f"**Status** {'RESOLVED' if resolved else 'FIRING'}",
        f"**Importance** {importance.upper()}",
        f"**Source** {event['source']}",
        "",
        str(event.get("summary") or event["title"]),
    ]
    body = str(event.get("body") or "").strip()
    if body:
        lines += ["", "---", body[:1500]]
    # Only a genuine fallback is worth flagging; "recovery" means the AI was
    # deliberately not asked, which is not a degradation.
    if event.get("route") == "rule":
        lines += ["", "_(rule-based triage: AI judgement unavailable)_"]
    # A recovery must never be mistakable for the alert it closes: same title,
    # same colour, same AI summary made the two indistinguishable in chat.
    title = ("[RESOLVED] " + event["title"]) if resolved else event["title"]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title[:100]},
                "template": "green" if resolved else _COLOR.get(importance, "blue"),
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        },
    }


async def send(client: httpx.AsyncClient, kind: str, url: str, payload: dict[str, Any]) -> None:
    """Deliver one message. Raises on any non-success so the caller can retry."""
    response = await client.post(url, json=payload, timeout=10.0)
    response.raise_for_status()
    if kind == "feishu":
        # Feishu answers 200 with a business error code; treat that as a failure
        # or a dead bot token would look like a successful delivery forever.
        data = response.json() if response.content else {}
        code = data.get("code", data.get("StatusCode", 0))
        if code:
            raise RuntimeError(f"feishu business error code={code}: {data.get('msg', '')}")
