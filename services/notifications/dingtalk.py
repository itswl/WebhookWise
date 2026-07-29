"""DingTalk robot notification formatting + target detection.

A forward rule whose target URL is a DingTalk robot webhook
(https://oapi.dingtalk.com/robot/send?access_token=...) is delivered as a
DingTalk markdown message instead of a raw JSON envelope. Zero configuration:
the URL itself selects the channel, mirroring how Feishu targets are detected.

Note: DingTalk robots typically enforce a keyword or signature security
setting; the message title always contains "告警通知", so a robot configured
with that keyword accepts these messages. Signed robots must embed the
timestamp/sign parameters in the rule's target URL themselves.
"""

from __future__ import annotations

from urllib.parse import urlparse

from contracts.webhook_payload import JsonObject, WebhookData
from services.notifications.markdown_summary import alert_markdown_summary, truncate_utf8
from services.webhooks.types import AnalysisResult

_DINGTALK_HOST = "oapi.dingtalk.com"
_DINGTALK_PATH_PREFIX = "/robot/send"

# DingTalk caps a robot message body at 20000 bytes; budget below it.
_MAX_TEXT_BYTES = 18000


def is_dingtalk_url(url: str) -> bool:
    try:
        parts = urlparse(str(url or "").strip())
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname == _DINGTALK_HOST and parts.path.startswith(_DINGTALK_PATH_PREFIX)


def build_dingtalk_markdown(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    *,
    is_periodic_reminder: bool = False,
) -> JsonObject:
    title, body = alert_markdown_summary(webhook_data, analysis_result, is_periodic_reminder=is_periodic_reminder)
    # DingTalk markdown wants the title repeated in the text for list previews.
    text = truncate_utf8(f"### {title}\n\n{body}", _MAX_TEXT_BYTES)
    return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
