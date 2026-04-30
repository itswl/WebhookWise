"""事件构建层 — 从 crud/webhook.py 提取的字段映射与对象构建逻辑。

crud/ 仅保留纯数据库操作（Repository Pattern），
字段映射、数据补全等业务辅助逻辑集中在此模块。
"""

from datetime import datetime
from typing import Any

from core.compression import compress_payload
from models import WebhookEvent

WebhookData = dict[str, Any]
HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]


def decode_raw_payload(raw_payload: bytes | None) -> bytes | None:
    """将原始 bytes payload 压缩为 gzip bytes。"""
    if not raw_payload:
        return None
    return compress_payload(raw_payload.decode("utf-8"))


def normalize_headers(headers: HeadersDict | None) -> HeadersDict:
    return dict(headers) if headers else {}


def fill_event_fields(
    event: WebhookEvent,
    *,
    source: str,
    client_ip: str | None,
    data: WebhookData,
    alert_hash: str,
    ai_analysis: AnalysisResult | None,
    importance: str | None,
    forward_status: str,
    is_duplicate: int,
    duplicate_of: int | None,
    duplicate_count: int,
    beyond_window: int,
    processing_status: str = "completed",
    last_notified_at: datetime | None = None,
    headers: HeadersDict | None = None,
    raw_payload: bytes | None = None,
) -> None:
    """统一将字段映射到 ORM 对象，集中维护字段赋值逻辑。"""
    event.source = source
    event.client_ip = client_ip
    event.timestamp = datetime.now()
    event.parsed_data = data
    event.alert_hash = alert_hash
    event.ai_analysis = ai_analysis
    event.importance = importance
    event.forward_status = forward_status
    event.is_duplicate = is_duplicate
    event.duplicate_of = duplicate_of
    event.duplicate_count = duplicate_count
    event.beyond_window = beyond_window
    event.processing_status = processing_status
    if last_notified_at is not None:
        event.last_notified_at = last_notified_at
    if headers is not None:
        event.headers = normalize_headers(headers)
    if raw_payload is not None:
        event.raw_payload = decode_raw_payload(raw_payload)


def build_event(
    *,
    source: str,
    client_ip: str | None,
    raw_payload: bytes | None,
    headers: HeadersDict | None,
    data: WebhookData,
    alert_hash: str,
    ai_analysis: AnalysisResult | None,
    importance: str | None,
    forward_status: str,
    is_duplicate: int,
    duplicate_of: int | None,
    duplicate_count: int,
    beyond_window: int,
    last_notified_at: datetime | None = None,
) -> WebhookEvent:
    event = WebhookEvent()
    fill_event_fields(
        event,
        source=source,
        client_ip=client_ip,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=importance,
        forward_status=forward_status,
        is_duplicate=is_duplicate,
        duplicate_of=duplicate_of,
        duplicate_count=duplicate_count,
        beyond_window=beyond_window,
        last_notified_at=last_notified_at,
        headers=headers,
        raw_payload=raw_payload,
    )
    return event
