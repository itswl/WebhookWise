"""Channel-neutral Markdown summary of an alert (DingTalk / WeCom bodies).

The Feishu card has its own rich interactive layout (feishu_cards.py); DingTalk
and WeCom bots take plain Markdown. This builds the same story — importance,
one-line summary, impact, identity, source/time footer — as a small Markdown
document both bot formats can embed. Card copy is Chinese by design (product
display for Chinese-facing channels), matching the Feishu card.
"""

from __future__ import annotations

from typing import Any

from contracts.webhook_payload import WebhookData
from services.notifications.markdown_safety import escape_lark_md
from services.webhooks.types import AnalysisResult

_IMPORTANCE_LABEL = {"high": "🔴 高", "critical": "🔴 高", "medium": "🟡 中", "low": "🟢 低"}

# Identity fragments come from upstream payloads; cap them so one long rule
# name cannot blow a bot's byte limit on its own.
_MAX_IDENTITY_CHARS = 120


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Cut at a UTF-8 byte budget without splitting a multi-byte character.

    Bot limits (WeCom 4096, DingTalk 20000) are BYTES; slicing by characters is
    a no-op guard for CJK content (3 bytes per char).
    """
    encoded = str(text or "").encode("utf-8")
    if len(encoded) <= max_bytes:
        return str(text or "")
    return encoded[:max_bytes].decode("utf-8", "ignore")


def _parsed(webhook_data: WebhookData) -> dict[str, Any]:
    parsed_obj = webhook_data.get("parsed_data") or webhook_data.get("body") or {}
    return parsed_obj if isinstance(parsed_obj, dict) else {}


def _strip_prefix(text: str, *labels: str) -> str:
    stripped = text.strip()
    for label in labels:
        for sep in ("：", ":"):
            prefix = f"{label}{sep}"
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip()
    return stripped


def alert_markdown_summary(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    *,
    is_periodic_reminder: bool = False,
) -> tuple[str, str]:
    """Return (title, markdown_body) for a normal alert notification."""
    importance = str(analysis_result.get("importance", "medium")).strip().lower()
    if "." in importance:
        importance = importance.rsplit(".", 1)[-1]
    label = _IMPORTANCE_LABEL.get(importance, "🟡 中")

    parsed = _parsed(webhook_data)
    source = str(webhook_data.get("source", "") or parsed.get("source", "") or "—")
    rule_name = str(parsed.get("RuleName", "") or parsed.get("alert_name", "") or "")[:_MAX_IDENTITY_CHARS]
    event_type = str(analysis_result.get("event_type") or parsed.get("event_type", "") or parsed.get("Type", "") or "")[
        :_MAX_IDENTITY_CHARS
    ]
    summary = _strip_prefix(str(analysis_result.get("summary", "")), "事件摘要", "摘要")
    impact = _strip_prefix(str(analysis_result.get("impact_scope", "")), "影响范围", "影响")
    timestamp = str(webhook_data.get("timestamp", "") or "")

    prefix = "🔁 [周期提醒] " if is_periodic_reminder else ""
    title = f"{prefix}📡 告警通知"

    # WeCom renders <@userid> mentions and <font> tags, both bots render
    # [label](url) links, and a ** in a value closes this template's own bold —
    # the same constructs the Feishu card escapes, so every payload- or
    # model-derived value goes through the same escape.
    lines = [f"**{label}　{escape_lark_md(summary[:400])}**" if summary else f"**{label}**"]
    identity_bits = [escape_lark_md(bit) for bit in (rule_name, event_type) if bit]
    if identity_bits:
        lines.append(f"🏷️ {' ・ '.join(identity_bits[:2])}")
    if impact:
        lines.append(f"**🎯 影响范围**：{escape_lark_md(impact[:600])}")
    lines.append(f"🔔 {escape_lark_md(source)} ・ 🕐 {escape_lark_md(timestamp) or '—'}")
    return title, "\n\n".join(lines)
