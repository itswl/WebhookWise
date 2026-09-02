"""The digest card: one message for a window of alerts, every quoted value inert."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from services.notifications.digest_cards import MAX_DIGEST_LINES, build_digest_card, build_digest_markdown
from services.notifications.dingtalk import build_dingtalk_digest
from services.notifications.markdown_safety import escape_lark_md
from services.notifications.wecom import build_wecom_digest

WINDOW_START = datetime(2026, 9, 2, 2, 0)  # 10:00 UTC+8
WINDOW_END = datetime(2026, 9, 2, 3, 0)
DEPOSIT = "示例充值超限告警"


def _record(
    *,
    minute: int,
    summary: str,
    importance: str = "medium",
    status: str = "firing",
    rule: str = DEPOSIT,
    source: str = "grafana",
) -> Any:
    return SimpleNamespace(
        id=minute,
        rule_name="ops chat",
        created_at=datetime(2026, 9, 2, 2, minute),
        forward_data={
            "source": source,
            "timestamp": f"2026-09-02T02:{minute:02d}:00Z",
            "parsed_data": {"RuleName": rule, "status": status},
        },
        analysis_result={"importance": importance, "summary": summary},
    )


def _body(card: dict[str, Any]) -> str:
    return "\n".join(
        str(element["text"]["content"]) for element in card["card"]["elements"] if element.get("tag") == "div"
    )


def test_escaping_makes_mentions_links_and_emphasis_inert() -> None:
    assert escape_lark_md("<at id=all></at>") == "＜at id=all＞＜/at＞"
    # The reviewed escaper keeps the brackets and breaks the link with a zero-width space.
    assert escape_lark_md("[x](http://evil.example)") == "[x]\u200b(http://evil.example)"
    assert escape_lark_md("**loud** ~~gone~~ `code`") == "＊＊loud＊＊ ～～gone～～ ˋcodeˋ"
    assert escape_lark_md(None) == ""
    # Ordinary prose, including the colon and full stop the model writes, is untouched.
    assert escape_lark_md("当前值：920.00，超过阈值 500。") == "当前值：920.00，超过阈值 500。"


def test_the_card_says_when_how_many_and_what_in_time_order() -> None:
    records = [
        _record(minute=45, summary="第三条"),
        _record(minute=5, summary="第一条", importance="low"),
        _record(minute=20, summary="第二条已恢复", status="resolved"),
    ]

    card = build_digest_card(records, window_start=WINDOW_START, window_end=WINDOW_END)

    header = card["card"]["header"]
    assert header["title"]["content"] == f"📦 汇总通知 · {DEPOSIT}"
    assert header["template"] == "orange"  # highest FIRING importance is medium
    body = _body(card)
    assert "🕐 2026-09-02 10:00 – 11:00 UTC+8" in body
    assert "共 3 条，其中 1 条恢复" in body
    lines = [line for line in body.splitlines() if line.startswith(("🔴", "🟢"))]
    assert lines == [
        "🔴 10:05 · 低 · 第一条",
        "🟢 10:20 · 已恢复 · 第二条已恢复",
        "🔴 10:45 · 中 · 第三条",
    ]
    note = card["card"]["elements"][-1]
    assert note["tag"] == "note"
    assert note["elements"][0]["content"] == "🔔 grafana ・ 📨 ops chat"


def test_the_header_takes_the_colour_of_the_highest_importance() -> None:
    records = [_record(minute=1, summary="a", importance="low"), _record(minute=2, summary="b", importance="high")]
    assert (
        build_digest_card(records, window_start=WINDOW_START, window_end=WINDOW_END)["card"]["header"]["template"]
        == "red"
    )

    only_low = [_record(minute=1, summary="a", importance="low")]
    assert (
        build_digest_card(only_low, window_start=WINDOW_START, window_end=WINDOW_END)["card"]["header"]["template"]
        == "green"
    )

    # A recovery of a high does not paint the header red: nothing is firing high.
    recovered = [
        _record(minute=1, summary="a", importance="high", status="resolved"),
        _record(minute=2, summary="b", importance="low"),
    ]
    assert (
        build_digest_card(recovered, window_start=WINDOW_START, window_end=WINDOW_END)["card"]["header"]["template"]
        == "green"
    )


def test_payload_and_model_text_cannot_page_the_group_or_plant_a_link() -> None:
    records = [
        _record(minute=1, summary="<at id=all></at> 大家看一下 [详情](http://evil.example) **紧急**"),
        _record(minute=2, summary="x", rule="<at id=all>rule</at>"),
    ]

    card = build_digest_card(records, window_start=WINDOW_START, window_end=WINDOW_END)
    body = _body(card)

    assert "<at" not in body and "](http" not in body and "**紧急**" not in body
    assert "＜at id=all＞" in body and "[详情]\u200b(http://evil.example)" in body
    # Two distinct rule names: the header counts them instead of quoting one.
    assert card["card"]["header"]["title"]["content"] == "📦 汇总通知 · 2 条规则"

    title, markdown = build_digest_markdown(records, window_start=WINDOW_START, window_end=WINDOW_END)
    assert "<at" not in markdown and "](http" not in markdown
    assert "<at" not in title


def test_long_summaries_are_cut_and_long_groups_are_counted_not_listed() -> None:
    long_summary = "很长的摘要" * 60
    records = [_record(minute=index, summary=f"{index:02d} {long_summary}") for index in range(MAX_DIGEST_LINES + 5)]

    card = build_digest_card(records, window_start=WINDOW_START, window_end=WINDOW_END)
    body = _body(card)
    lines = [line for line in body.splitlines() if line.startswith("🔴")]

    assert len(lines) == MAX_DIGEST_LINES
    assert lines[0].startswith("🔴 10:00 · 中 · 00 很长") and lines[-1].startswith("🔴 10:14 · 中 · 14 很长")
    assert all(len(line.split(" · ", 2)[2]) <= 120 for line in lines)
    assert all(line.endswith("…") for line in lines)
    assert "…另有 5 条" in body
    assert f"共 {MAX_DIGEST_LINES + 5} 条" in body


def test_the_markdown_variant_tells_the_same_story_for_both_bots() -> None:
    records = [_record(minute=5, summary="第一条"), _record(minute=20, summary="第二条", status="resolved")]

    title, body = build_digest_markdown(records, window_start=WINDOW_START, window_end=WINDOW_END)

    assert title == f"📦 汇总通知 · {DEPOSIT}"
    assert "🕐 2026-09-02 10:00 – 11:00 UTC+8" in body
    assert "**共 2 条，其中 1 条恢复**" in body
    assert "- 🔴 10:05 · 中 · 第一条" in body and "- 🟢 10:20 · 已恢复 · 第二条" in body
    assert body.rstrip().endswith("🔔 grafana ・ 📨 ops chat")

    dingtalk = build_dingtalk_digest(records, window_start=WINDOW_START, window_end=WINDOW_END)
    assert dingtalk["msgtype"] == "markdown"
    assert dingtalk["markdown"]["title"] == title
    assert dingtalk["markdown"]["text"].startswith(f"### {title}")

    wecom = build_wecom_digest(records, window_start=WINDOW_START, window_end=WINDOW_END)
    assert wecom["msgtype"] == "markdown"
    assert wecom["markdown"]["content"].startswith(f"### {title}")
    assert len(wecom["markdown"]["content"].encode("utf-8")) <= 4096


def test_a_window_spanning_midnight_shows_both_dates() -> None:
    card = build_digest_card(
        [_record(minute=1, summary="a")],
        window_start=datetime(2026, 9, 2, 15, 0),  # 23:00 UTC+8
        window_end=datetime(2026, 9, 2, 16, 0),  # 00:00 UTC+8 next day
    )
    assert "🕐 2026-09-02 23:00 – 2026-09-03 00:00 UTC+8" in _body(card)
