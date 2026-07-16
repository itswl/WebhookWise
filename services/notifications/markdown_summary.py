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
from services.webhooks.types import AnalysisResult

_IMPORTANCE_LABEL = {"high": "🔴 高", "critical": "🔴 高", "medium": "🟡 中", "low": "🟢 低"}


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
    rule_name = str(parsed.get("RuleName", "") or parsed.get("alert_name", "") or "")
    event_type = str(analysis_result.get("event_type") or parsed.get("event_type", "") or parsed.get("Type", "") or "")
    summary = _strip_prefix(str(analysis_result.get("summary", "")), "事件摘要", "摘要")
    impact = _strip_prefix(str(analysis_result.get("impact_scope", "")), "影响范围", "影响")
    timestamp = str(webhook_data.get("timestamp", "") or "")

    prefix = "🔁 [周期提醒] " if is_periodic_reminder else ""
    title = f"{prefix}📡 告警通知"

    lines = [f"**{label}　{summary[:400]}**" if summary else f"**{label}**"]
    identity_bits = [bit for bit in (rule_name, event_type) if bit]
    if identity_bits:
        lines.append(f"🏷️ {' ・ '.join(identity_bits[:2])}")
    if impact:
        lines.append(f"**🎯 影响范围**：{impact[:600]}")
    lines.append(f"🔔 {source} ・ 🕐 {timestamp or '—'}")
    return title, "\n\n".join(lines)
