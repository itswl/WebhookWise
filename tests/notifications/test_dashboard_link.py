"""Chat cards link back to the decision chain when the dashboard URL is known."""

from __future__ import annotations

import json

import pytest

from services.notifications import feishu_cards
from services.operations import runtime_settings as rt

WEBHOOK = {
    "source": "grafana",
    "parsed_data": {"RuleName": "demo rule", "status": "firing"},
    "timestamp": "2026-09-03T00:00:00Z",
}
ANALYSIS = {"importance": "medium", "summary": "demo summary", "event_type": "告警转发"}


def _links(card: dict) -> list[str]:
    return [
        e["text"]["content"] for e in card["card"]["elements"] if e.get("tag") == "div" and "](" in e["text"]["content"]
    ]


def test_no_url_means_no_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "override_or", lambda key, fallback: "" if key == "DASHBOARD_PUBLIC_URL" else fallback)
    card = feishu_cards.build_feishu_card(WEBHOOK, ANALYSIS, event_id=42)  # type: ignore[arg-type]
    assert _links(card) == []


def test_configured_url_links_to_the_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rt,
        "override_or",
        lambda key, fallback: "https://alerts.example.com/" if key == "DASHBOARD_PUBLIC_URL" else fallback,
    )
    card = feishu_cards.build_feishu_card(WEBHOOK, ANALYSIS, event_id=42)  # type: ignore[arg-type]
    assert _links(card) == ["[🔎 查看决策链](https://alerts.example.com/#/alerts/42)"]
    # The link is markup, not payload text: it survives as a real link.
    assert "\u200b" not in json.dumps(card)


def test_unknown_event_id_adds_no_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rt,
        "override_or",
        lambda key, fallback: "https://alerts.example.com" if key == "DASHBOARD_PUBLIC_URL" else fallback,
    )
    card = feishu_cards.build_feishu_card(WEBHOOK, ANALYSIS)  # type: ignore[arg-type]
    assert _links(card) == []


def test_public_url_caster_rejects_non_http() -> None:
    from services.operations.runtime_settings import _cast_public_url

    assert _cast_public_url(" https://alerts.example.com/ ") == "https://alerts.example.com"
    assert _cast_public_url("") == ""
    with pytest.raises(ValueError):
        _cast_public_url("alerts.example.com")
