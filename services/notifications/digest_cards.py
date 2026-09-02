"""One chat message for a window of alerts from a digested rule.

An inbound ``digest`` rule turns "one card per alert" into "one card per window
per rule" for chat targets. This builds that card: the Feishu interactive
variant and the Markdown variant DingTalk / WeCom bots take. Card copy is
Chinese by design, matching the alert card.

Every value that came from a payload or a model — rule names, summaries,
sources — passes through :func:`escape_lark_md` before it is rendered. The
alert card quotes one summary; this quotes fifteen, from fifteen alerts, so the
"payload text is data" doctrine has to hold for each line.

There are deliberately no per-alert links: the service has no public URL
setting, and a link nobody can open is worse than none.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from contracts.webhook_payload import JsonObject
from core.datetime_utils import naive_utc, parse_utc_datetime
from services.notifications.feishu_cards import _CHINA_TZ, _IMPORTANCE_TEMPLATE, _strip_redundant_prefix
from services.notifications.markdown_safety import escape_lark_md
from services.webhooks.inbound_rules import alert_rule_name

# Fifteen lines is a card a person still reads; past that, "and K more" says
# what matters — the volume — without becoming a log file in a chat window.
MAX_DIGEST_LINES: Final = 15
_SUMMARY_MAX_CHARS: Final = 120
_TITLE_MAX_CHARS: Final = 60
_IMPORTANCE_RANK: Final = {"low": 0, "medium": 1, "high": 2, "critical": 3}
# The alert card's labels carry a glyph ("🔴 高"); a digest line already leads
# with the firing/recovery glyph, so the label here is the word alone.
_IMPORTANCE_LABEL: Final = {"high": "高", "critical": "严重", "medium": "中", "low": "低"}
_UNKNOWN_IMPORTANCE_LABEL: Final = "未知"
_FIRING_GLYPH: Final = "🔴"
_RECOVERY_GLYPH: Final = "🟢"
_DIGEST_TITLE: Final = "📦 汇总通知"


@dataclass(frozen=True, slots=True)
class DigestLine:
    """One alert, reduced to what a digest line shows."""

    at: datetime | None  # naive UTC
    importance: str
    is_recovery: bool
    summary: str  # escaped, single line, truncated
    order: int  # tie-breaker when two alerts share a timestamp


@dataclass(frozen=True, slots=True)
class DigestContent:
    """The card's text, channel-neutral; the two builders lay it out."""

    title: str
    template: str
    window_text: str
    count_text: str
    lines: tuple[str, ...]
    overflow: int
    footer: str


def _single_line(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _parsed(forward_data: dict[str, Any]) -> dict[str, Any]:
    return _mapping(forward_data.get("parsed_data") or forward_data.get("body") or {})


def _normalized_importance(analysis: dict[str, Any]) -> str:
    importance = str(analysis.get("importance") or "").strip().lower()
    if "." in importance:
        importance = importance.rsplit(".", 1)[-1]
    return importance


def _event_time(record: Any, forward_data: dict[str, Any]) -> datetime | None:
    stamped = parse_utc_datetime(str(forward_data.get("timestamp") or "") or None)
    if stamped is not None:
        return stamped
    created = getattr(record, "created_at", None)
    return naive_utc(created) if isinstance(created, datetime) else None


def _local(value: datetime) -> datetime:
    return naive_utc(value).replace(tzinfo=UTC).astimezone(_CHINA_TZ)


def _format_window(window_start: datetime, window_end: datetime) -> str:
    start, end = _local(window_start), _local(window_end)
    if start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M} – {end:%H:%M} UTC+8"
    return f"{start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M} UTC+8"


def _line_for(record: Any, order: int) -> DigestLine:
    from services.incidents.grouping import is_recovery_payload

    forward_data = _mapping(getattr(record, "forward_data", None))
    analysis = _mapping(getattr(record, "analysis_result", None))
    summary = _strip_redundant_prefix(_single_line(analysis.get("summary")), "事件摘要", "摘要")
    if not summary:
        summary = _single_line(alert_rule_name(_parsed(forward_data))) or "—"
    return DigestLine(
        at=_event_time(record, forward_data),
        importance=_normalized_importance(analysis),
        is_recovery=is_recovery_payload(_parsed(forward_data), analysis),
        summary=escape_lark_md(_truncate(summary, _SUMMARY_MAX_CHARS)),
        order=order,
    )


def _render_line(line: DigestLine) -> str:
    glyph = _RECOVERY_GLYPH if line.is_recovery else _FIRING_GLYPH
    label = "已恢复" if line.is_recovery else _IMPORTANCE_LABEL.get(line.importance, _UNKNOWN_IMPORTANCE_LABEL)
    when = f"{_local(line.at):%H:%M}" if line.at is not None else "--:--"
    return f"{glyph} {when} · {label} · {line.summary}"


def _highest_importance(lines: Sequence[DigestLine]) -> str:
    """The most severe FIRING importance in the group; recoveries do not colour a header."""
    firing = [line.importance for line in lines if not line.is_recovery]
    ranked = sorted(firing, key=lambda value: _IMPORTANCE_RANK.get(value, -1), reverse=True)
    return ranked[0] if ranked else ""


def _title_for(records: Sequence[Any]) -> str:
    names = {
        name
        for name in (
            _single_line(alert_rule_name(_parsed(_mapping(getattr(record, "forward_data", None)))))
            for record in records
        )
        if name
    }
    if not names:
        return _DIGEST_TITLE
    if len(names) == 1:
        return f"{_DIGEST_TITLE} · {_truncate(next(iter(names)), _TITLE_MAX_CHARS)}"
    return f"{_DIGEST_TITLE} · {len(names)} 条规则"


def _footer_for(records: Sequence[Any]) -> str:
    sources = sorted(
        {
            _single_line(_mapping(getattr(record, "forward_data", None)).get("source"))
            for record in records
            if _single_line(_mapping(getattr(record, "forward_data", None)).get("source"))
        }
    )
    forward_rule = _single_line(getattr(records[0], "rule_name", "")) if records else ""
    return f"🔔 {_truncate(' / '.join(sources), 80) or '—'} ・ 📨 {_truncate(forward_rule, 80) or '—'}"


def digest_content(records: Sequence[Any], *, window_start: datetime, window_end: datetime) -> DigestContent:
    """Reduce the group's records to the text both card variants render."""
    lines = sorted(
        (_line_for(record, order) for order, record in enumerate(records)),
        key=lambda line: (line.at or datetime.max, line.order),
    )
    recoveries = sum(1 for line in lines if line.is_recovery)
    count_text = f"共 {len(lines)} 条"
    if recoveries:
        count_text += f"，其中 {recoveries} 条恢复"
    shown = lines[:MAX_DIGEST_LINES]
    return DigestContent(
        title=_title_for(records),
        template=_IMPORTANCE_TEMPLATE.get(_highest_importance(lines), "orange"),
        window_text=_format_window(window_start, window_end),
        count_text=count_text,
        lines=tuple(_render_line(line) for line in shown),
        overflow=len(lines) - len(shown),
        footer=_footer_for(records),
    )


def build_digest_card(records: Sequence[Any], *, window_start: datetime, window_end: datetime) -> JsonObject:
    """The Feishu interactive card for one digest window."""
    content = digest_content(records, window_start=window_start, window_end=window_end)
    elements: list[JsonObject] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"🕐 {content.window_text}\n**{content.count_text}**"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(content.lines) or "—"}},
    ]
    if content.overflow:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"…另有 {content.overflow} 条"}})
    elements.append({"tag": "hr"})
    from services.notifications.feishu_cards import _link_element, dashboard_public_url

    base = dashboard_public_url()
    if base:
        elements.append(_link_element(f"{base}/#/alerts", "🔎 打开告警列表"))
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": content.footer}]})
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": content.title}, "template": content.template},
            "elements": elements,
        },
    }


def build_digest_markdown(records: Sequence[Any], *, window_start: datetime, window_end: datetime) -> tuple[str, str]:
    """(title, markdown body) for the DingTalk / WeCom bot variants."""
    content = digest_content(records, window_start=window_start, window_end=window_end)
    sections = [
        f"🕐 {content.window_text}",
        f"**{content.count_text}**",
        "\n".join(f"- {line}" for line in content.lines) or "—",
    ]
    if content.overflow:
        sections.append(f"…另有 {content.overflow} 条")
    sections.append(escape_lark_md(content.footer))
    return escape_lark_md(content.title), "\n\n".join(sections)
