"""WeCom (企业微信) group-bot notification formatting + target detection.

A forward rule whose target URL is a WeCom bot webhook
(https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...) is delivered as a
WeCom markdown message instead of a raw JSON envelope. The URL selects the
channel — same zero-config detection as Feishu and DingTalk targets.
"""

from __future__ import annotations

from urllib.parse import urlparse

from contracts.webhook_payload import JsonObject, WebhookData
from services.notifications.markdown_summary import alert_markdown_summary, truncate_utf8
from services.webhooks.types import AnalysisResult

_WECOM_HOST = "qyapi.weixin.qq.com"
_WECOM_PATH_PREFIX = "/cgi-bin/webhook/send"

# WeCom rejects markdown content above 4096 BYTES; budget below that. The cut
# is byte-aware — a character count is a no-op guard for CJK (3 bytes/char).
_MAX_CONTENT_BYTES = 4000


def is_wecom_url(url: str) -> bool:
    try:
        parts = urlparse(str(url or "").strip())
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname == _WECOM_HOST and parts.path.startswith(_WECOM_PATH_PREFIX)


def build_wecom_markdown(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    *,
    is_periodic_reminder: bool = False,
) -> JsonObject:
    title, body = alert_markdown_summary(webhook_data, analysis_result, is_periodic_reminder=is_periodic_reminder)
    content = f"### {title}\n\n{body}"
    return {"msgtype": "markdown", "markdown": {"content": truncate_utf8(content, _MAX_CONTENT_BYTES)}}
