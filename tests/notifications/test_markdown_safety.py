"""Untrusted text in chat cards: mentions, links and emphasis must render inert.

A crafted alert body or a prompt-injected analysis reaches the operator's card
verbatim; these tests pin that it can neither page the chat, hide a link behind
a label, nor close the card's own bold labels — and that the card copy itself
(labels, verdict, recovery treatment) is untouched by the escape.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from contracts.deep_analysis_report import DEEP_ANALYSIS_REPORT_SCHEMA
from contracts.webhook_payload import WebhookData
from services.notifications.dingtalk import build_dingtalk_markdown
from services.notifications.feishu_cards import (
    build_ai_error_card,
    build_deep_analysis_card,
    build_delivery_exhausted_card,
    build_feishu_card,
)
from services.notifications.markdown_safety import escape_lark_md
from services.notifications.wecom import build_wecom_markdown
from services.webhooks.types import AnalysisResult

_MENTION = "<at id=all></at>"
_LINK = "[click](http://evil.example)"
_INJECTED = f"磁盘告警 {_MENTION} {_LINK} **假标题** ~~x~~ `rm -rf`"


def _lark_md_contents(card: dict[str, Any]) -> list[str]:
    """Every lark_md string in the card, in render order (div text and field grids)."""
    contents: list[str] = []
    for element in card["card"]["elements"]:
        text = element.get("text")
        if isinstance(text, dict) and text.get("tag") == "lark_md":
            contents.append(str(text["content"]))
        contents.extend(str(field["text"]["content"]) for field in element.get("fields") or [])
    return contents


def _assert_inert(content: str) -> None:
    assert "<" not in content and ">" not in content, content
    assert "](" not in content, content
    assert "`" not in content and "~~" not in content, content


def _alert_card(
    analysis: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
    kb_links: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    webhook = {"source": "grafana", "timestamp": "2026-09-01T00:00:00Z", "parsed_data": parsed or {}}
    return build_feishu_card(cast(WebhookData, webhook), cast(AnalysisResult, analysis), kb_links=kb_links)


def test_escape_lark_md_neutralizes_the_dangerous_constructs_and_nothing_else() -> None:
    escaped = escape_lark_md(_INJECTED)
    _assert_inert(escaped)
    assert "**" not in escaped
    # Readable: the words survive; the markup becomes full-width look-alikes.
    assert "＜at id=all＞＜/at＞" in escaped
    assert "[click]​(http://evil.example)" in escaped
    assert "＊＊假标题＊＊" in escaped and "～～x～～" in escaped and "ˋrm -rfˋ" in escaped
    # Ordinary alert prose — a lone * or ~, CJK, digits — passes through unchanged.
    plain = "CPU 使用率 95%，磁盘 * 剩余 ~3%"
    assert escape_lark_md(plain) == plain
    assert escape_lark_md("") == ""


def test_alert_card_keeps_its_own_bold_but_not_the_payloads() -> None:
    card = _alert_card({"importance": "high", "summary": _INJECTED, "impact_scope": f"**🎯 影响范围**\n{_MENTION}"})
    headline, *rest = _lark_md_contents(card)
    _assert_inert(headline)
    # The template's bold still wraps the whole headline: the injected ** could
    # not close it early, so the fake "假标题" label is not bold.
    assert headline.startswith("**🔴 高　磁盘告警 ") and headline.endswith("**")
    assert headline.count("**") == 2
    assert "click" in headline and "http://evil.example" in headline
    impact = next(content for content in rest if content.startswith("**🎯 影响范围**\n"))
    _assert_inert(impact)
    assert impact.count("**") == 2  # only the section title is bold


def test_identity_line_and_knowledge_base_links_are_escaped() -> None:
    card = _alert_card(
        {"importance": "medium", "summary": "s"},
        parsed={"RuleName": f"rule {_MENTION}", "Level": f"P1 {_LINK}"},
        kb_links=[{"title": f"**{_LINK}**", "snippet": _MENTION}],
    )
    contents = _lark_md_contents(card)
    identity = next(content for content in contents if content.startswith("🏷️ "))
    _assert_inert(identity)
    assert "rule ＜at id=all＞＜/at＞" in identity
    kb = next(content for content in contents if content.startswith("**📖 相关知识库**\n"))
    _assert_inert(kb)
    # Section title + one bold title per entry; the entry's own ** were neutralized.
    assert kb.count("**") == 4
    assert "click" in kb and "http://evil.example" in kb


def test_deep_analysis_card_escapes_model_output_in_every_section() -> None:
    report = {
        "schema": DEEP_ANALYSIS_REPORT_SCHEMA,
        "summary": _INJECTED,
        "root_cause": f"{_MENTION} 根因",
        "impact": _LINK,
        "recommendations": [f"**{_MENTION}**", _LINK],
        "evidence": ["<font color='red'>红</font>"],
        "next_checks": ["`curl` http://x"],
        "confidence": 0.5,
        "alert_identity": {"source": "volcengine", "rule_name": f"r {_MENTION}"},
    }
    card = build_deep_analysis_card(
        {"normalized_report": report, "engine": "hookprobe", "duration_seconds": 1.0},
        source=f"volcengine {_MENTION}",
        webhook_event_id=1,
    )
    contents = _lark_md_contents(card)
    assert len(contents) >= 10  # 4 header fields + summary, root cause, impact, 3 lists, identity
    for content in contents:
        _assert_inert(content)
        # Each block is "**title**\n<body>": exactly one bold pair, the template's.
        assert content.count("**") == 2, content
    assert any("＜at id=all＞＜/at＞ 根因" in content for content in contents)
    assert any("- ＊＊＜at id=all＞＜/at＞＊＊" in content for content in contents)


def test_delivery_exhausted_and_ai_error_cards_escape_error_text() -> None:
    outbox = SimpleNamespace(
        id=5,
        webhook_event_id=9,
        target_type="webhook",
        channel_name="webhook",
        target_url="https://example.test/hook",
        attempts=3,
        max_attempts=3,
        last_error=_INJECTED,
    )
    exhausted = build_delivery_exhausted_card(outbox)
    error_block = next(c for c in _lark_md_contents(exhausted) if c.startswith("**⚠️ 最后错误**\n"))
    _assert_inert(error_block)
    assert error_block.count("**") == 2

    ai_error = build_ai_error_card(cast(WebhookData, {"source": f"grafana {_MENTION}"}), _INJECTED)
    (content,) = _lark_md_contents(ai_error)
    _assert_inert(content)
    assert content.count("**") == 4  # the 来源 and 原因 labels, nothing else


def test_dingtalk_and_wecom_bodies_are_escaped_too() -> None:
    webhook = cast(
        WebhookData,
        {
            "source": f"grafana {_MENTION}",
            "timestamp": "2026-09-01T00:00:00Z",
            "parsed_data": {"RuleName": f"rule <@all> {_LINK}"},
        },
    )
    analysis = cast(AnalysisResult, {"importance": "high", "summary": _INJECTED, "impact_scope": _LINK})
    dingtalk = str(build_dingtalk_markdown(webhook, analysis)["markdown"]["text"])  # type: ignore[index]
    wecom = str(build_wecom_markdown(webhook, analysis)["markdown"]["content"])  # type: ignore[index]
    for text in (dingtalk, wecom):
        title, body = text.split("\n\n", 1)
        assert title == "### 📡 告警通知"  # the template's heading is the only heading
        _assert_inert(body)
        assert "<@all>" not in body
        # Headline + 影响范围 are the template's two bold pairs; the payload added none.
        assert body.startswith("**🔴 高　磁盘告警 ")
        assert body.count("**") == 4


def test_recovery_and_verdict_copy_are_unchanged_by_the_escape() -> None:
    firing = _alert_card(
        {"importance": "high", "summary": f"充值 {_MENTION}", "triage_verdict": "act_now", "triage_confidence": 0.82},
        parsed={"state": "alerting", "title": "[alerting] x"},
    )
    contents = _lark_md_contents(firing)
    assert firing["card"]["header"]["template"] == "red"
    assert contents[0] == "**🔴 高　充值 ＜at id=all＞＜/at＞**"
    assert "⚡ 处置建议：**立即处理**（置信度 82%）" in contents

    recovered = _alert_card(
        {"importance": "high", "summary": f"充值 {_MENTION}"}, parsed={"state": "ok", "title": "[ok] x"}
    )
    assert recovered["card"]["header"]["template"] == "green"
    assert _lark_md_contents(recovered)[0] == "**✅ 已恢复　充值 ＜at id=all＞＜/at＞**"
