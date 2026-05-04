"""业务协调层 — 处理 Webhook 的保存、重复检测与协调。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import orjson
from fastapi import Request
from sqlalchemy import column, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from core.compression import COMPRESS_THRESHOLD_BYTES, compress_payload
from core.config import Config
from core.logger import logger
from core.utils import generate_alert_hash
from db.session import session_scope
from models import WebhookEvent
from services.dedup_strategy import _resolve_analysis_for_duplicate, check_duplicate_alert
from services.event_builder import (
    AnalysisResult,
    HeadersDict,
    WebhookData,
    build_event,
    decode_raw_payload,
    fill_event_fields,
    normalize_headers,
)
from services.file_backup import get_webhooks_from_files, save_webhook_to_file


@dataclass(frozen=True)
class SaveWebhookResult:
    webhook_id: int | str
    is_duplicate: bool
    original_id: int | None
    beyond_window: bool


async def quick_receive_webhook(
    session: AsyncSession,
    source: str,
    raw_headers: dict,
    raw_body: str | bytes,
    parsed_data: dict | None = None,
) -> int:
    """同步最小化写入：仅持久化原始数据，不做任何分析/转发。"""
    raw_text = raw_body if isinstance(raw_body, str) else raw_body.decode("utf-8", errors="replace")
    body_len = len(raw_body) if isinstance(raw_body, bytes) else len(raw_body.encode("utf-8"))
    if body_len > COMPRESS_THRESHOLD_BYTES:
        compressed = await asyncio.to_thread(compress_payload, raw_text)
    else:
        compressed = compress_payload(raw_text)
    stmt = (
        insert(WebhookEvent)
        .values(
            source=source,
            headers=raw_headers if isinstance(raw_headers, dict) else orjson.loads(raw_headers),
            raw_payload=compressed,
            parsed_data=parsed_data,
            processing_status="received",
        )
        .returning(WebhookEvent.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


def get_client_ip(request: Request) -> str:
    """获取客户端 IP 地址"""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for: return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip: return real_ip
    return request.client.host if request.client else "unknown"


# ── 投影查询逻辑 ──

_SUMMARY_COLUMNS = [
    WebhookEvent.id, WebhookEvent.source, WebhookEvent.client_ip, WebhookEvent.timestamp,
    WebhookEvent.importance, WebhookEvent.is_duplicate, WebhookEvent.duplicate_of,
    WebhookEvent.duplicate_count, WebhookEvent.beyond_window, WebhookEvent.forward_status,
    WebhookEvent.ai_analysis, WebhookEvent.parsed_data, WebhookEvent.created_at, WebhookEvent.prev_alert_id,
]


def _row_to_summary_dict(row) -> dict:
    from adapters.summary_extractors import extract_summary_fields
    ai_analysis = row.ai_analysis
    beyond_window, is_dup = bool(row.beyond_window), bool(row.is_duplicate)
    return {
        "id": row.id, "source": row.source, "client_ip": row.client_ip,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "importance": row.importance, "is_duplicate": is_dup, "duplicate_of": row.duplicate_of,
        "duplicate_count": row.duplicate_count, "beyond_window": beyond_window,
        "forward_status": row.forward_status, "summary": ai_analysis.get("summary", "") if ai_analysis else None,
        "alert_info": extract_summary_fields(row.source, row.parsed_data),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "prev_alert_id": row.prev_alert_id, "beyond_time_window": beyond_window,
        "is_within_window": is_dup and not beyond_window,
        "duplicate_type": ("beyond_window" if beyond_window else "within_window") if is_dup else "new",
    }


async def list_webhook_summaries(session: AsyncSession, *, cursor_id: int | None = None, importance: str = "", source: str = "", page_size: int = 20) -> tuple[list[dict], bool, int | None]:
    query = select(*_SUMMARY_COLUMNS)
    if cursor_id is not None: query = query.where(WebhookEvent.id < cursor_id)
    if importance: query = query.where(WebhookEvent.importance == importance)
    if source: query = query.where(WebhookEvent.source == source)
    query = query.order_by(WebhookEvent.id.desc()).limit(page_size + 1)
    result = await session.execute(query); rows = result.all()
    has_more = len(rows) > page_size
    if has_more: rows = rows[:page_size]
    items = [_row_to_summary_dict(r) for r in rows]
    return items, has_more, (rows[-1].id if has_more and rows else None)


async def list_webhook_summaries_cursor(session: AsyncSession, *, cursor_id: int | None = None, importance: str = "", source: str = "", limit: int = 200) -> tuple[list[dict], bool, int | None]:
    query = select(*_SUMMARY_COLUMNS)
    if importance: query = query.where(WebhookEvent.importance == importance)
    if source: query = query.where(WebhookEvent.source == source)
    if cursor_id is not None: query = query.where(WebhookEvent.id < cursor_id)
    query = query.order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc()).limit(limit)
    result = await session.execute(query); rows = result.all()
    has_more = len(rows) == limit
    items = [_row_to_summary_dict(r) for r in rows]
    return items, has_more, (rows[-1].id if has_more and rows else None)


async def get_all_webhooks(page: int = 1, page_size: int = 20, cursor_id: int | None = None, fields: str = "summary") -> tuple[list[dict], int, int | None]:
    """向后兼容接口：获取所有 webhooks"""
    async with session_scope() as session:
        items, has_more, next_cursor = await list_webhook_summaries(session, cursor_id=cursor_id, page_size=page_size)
        return items, -1, next_cursor


# ── Dead Letter & Stuck Events ──

async def list_dead_letters(session: AsyncSession, page: int = 1, page_size: int = 20) -> list[dict]:
    stmt = select(WebhookEvent.id, WebhookEvent.source, WebhookEvent.timestamp, WebhookEvent.alert_hash, WebhookEvent.importance, WebhookEvent.retry_count, WebhookEvent.processing_status).where(WebhookEvent.processing_status == "dead_letter").order_by(WebhookEvent.id.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt); return [dict(row._mapping) for row in result.all()]


async def count_dead_letters(session: AsyncSession) -> int | None:
    from db.session import count_with_timeout
    return await count_with_timeout(session, select(func.count()).select_from(WebhookEvent).where(WebhookEvent.processing_status == "dead_letter"))


async def replay_dead_letter(session: AsyncSession, event_id: int) -> bool:
    stmt = update(WebhookEvent).where(WebhookEvent.id == event_id, WebhookEvent.processing_status == "dead_letter").values(processing_status="received", retry_count=0).returning(WebhookEvent.id)
    res = await session.execute(stmt); return res.scalar_one_or_none() is not None


async def list_stuck_events(session: AsyncSession, *, statuses: list[str] | None = None, older_than_seconds: int = 300, limit: int = 50) -> list[dict]:
    threshold = datetime.now() - timedelta(seconds=max(0, older_than_seconds))
    stmt = select(WebhookEvent.id, WebhookEvent.source, WebhookEvent.created_at, WebhookEvent.updated_at, WebhookEvent.retry_count, WebhookEvent.processing_status).where(WebhookEvent.processing_status.in_(statuses or ["received", "analyzing", "failed"]), WebhookEvent.created_at < threshold).order_by(WebhookEvent.created_at.asc()).limit(limit)
    res = await session.execute(stmt); return [dict(row._mapping) for row in res.all()]


async def requeue_stuck_event(session: AsyncSession, event_id: int) -> bool:
    stmt = update(WebhookEvent).where(WebhookEvent.id == event_id, WebhookEvent.processing_status.in_(["received", "analyzing", "failed"])).values(processing_status="received")
    res = await session.execute(stmt); return bool(res.rowcount)


# ── 保存逻辑 ──


async def _save_duplicate_event(
    session: AsyncSession,
    *,
    source: str,
    client_ip: str | None,
    raw_payload: bytes | None,
    headers: HeadersDict | None,
    data: WebhookData,
    alert_hash: str,
    ai_analysis: AnalysisResult | None,
    forward_status: str,
    original_event: WebhookEvent,
    beyond_window: bool,
    reanalyzed: bool,
    event_id: int | None = None,
) -> SaveWebhookResult | None:
    original = await session.get(WebhookEvent, original_event.id)
    if not original: return None
    original.duplicate_count = (original.duplicate_count or 1) + 1
    original.updated_at = datetime.now()
    final_ai_analysis, final_importance = _resolve_analysis_for_duplicate(ai_analysis, original, reanalyzed)

    if event_id is not None:
        dup_event = await session.get(WebhookEvent, event_id)
        if dup_event:
            fill_event_fields(dup_event, source=source, client_ip=client_ip, data=data, alert_hash=alert_hash, ai_analysis=final_ai_analysis, importance=final_importance, forward_status=forward_status, is_duplicate=1, duplicate_of=original.id, duplicate_count=original.duplicate_count, beyond_window=1 if beyond_window else 0, headers=headers, raw_payload=raw_payload)
            await session.flush()
            return SaveWebhookResult(dup_event.id, True, original.id, beyond_window)

    duplicate_event = build_event(source=source, client_ip=client_ip, raw_payload=raw_payload, headers=headers, data=data, alert_hash=alert_hash, ai_analysis=final_ai_analysis, importance=final_importance, forward_status=forward_status, is_duplicate=1, duplicate_of=original.id, duplicate_count=original.duplicate_count, beyond_window=1 if beyond_window else 0)
    session.add(duplicate_event); await session.flush()
    return SaveWebhookResult(duplicate_event.id, True, original.id, beyond_window)


async def _update_existing_event(
    session: AsyncSession,
    *,
    event_id: int,
    source: str,
    client_ip: str | None,
    raw_payload: bytes | None,
    headers: HeadersDict | None,
    data: WebhookData,
    alert_hash: str,
    ai_analysis: AnalysisResult | None,
    forward_status: str,
) -> SaveWebhookResult:
    event = await session.get(WebhookEvent, event_id)
    if not event: return await _save_new_event(session, source=source, client_ip=client_ip, raw_payload=raw_payload, headers=headers, data=data, alert_hash=alert_hash, ai_analysis=ai_analysis, forward_status=forward_status)
    fill_event_fields(event, source=source, client_ip=client_ip, data=data, alert_hash=alert_hash, ai_analysis=ai_analysis, importance=ai_analysis.get("importance") if ai_analysis else None, forward_status=forward_status, is_duplicate=0, duplicate_of=None, duplicate_count=1, beyond_window=0, last_notified_at=datetime.now(), headers=headers, raw_payload=raw_payload)
    await session.flush()
    return SaveWebhookResult(event.id, False, None, False)


async def _save_new_event(session: AsyncSession, **kwargs) -> SaveWebhookResult:
    event = build_event(**kwargs, is_duplicate=0, duplicate_count=1, beyond_window=0, last_notified_at=datetime.now())
    session.add(event); await session.flush()
    return SaveWebhookResult(event.id, False, None, False)


async def _upsert_new_event(
    session: AsyncSession,
    *,
    source: str,
    client_ip: str | None,
    raw_payload: bytes | None,
    headers: HeadersDict | None,
    data: WebhookData,
    alert_hash: str,
    ai_analysis: AnalysisResult | None,
    forward_status: str,
    beyond_window: bool,
) -> SaveWebhookResult:
    now = datetime.now()
    stmt = pg_insert(WebhookEvent).values(source=source, client_ip=client_ip, timestamp=now, raw_payload=decode_raw_payload(raw_payload), headers=normalize_headers(headers), parsed_data=data, alert_hash=alert_hash, ai_analysis=ai_analysis, importance=ai_analysis.get("importance") if ai_analysis else None, processing_status="completed", forward_status=forward_status, is_duplicate=0, duplicate_count=1, beyond_window=0, last_notified_at=now).on_conflict_do_update(index_elements=["alert_hash"], index_where=(WebhookEvent.is_duplicate == 0), set_={"duplicate_count": WebhookEvent.duplicate_count + 1, "updated_at": now}).returning(WebhookEvent.id, WebhookEvent.duplicate_count, column("xmax"))
    res = await session.execute(stmt); row = res.one()
    if row[2] == 0: return SaveWebhookResult(row[0], False, None, False)
    # 冲突降级
    dup = build_event(source=source, client_ip=client_ip, raw_payload=raw_payload, headers=headers, data=data, alert_hash=alert_hash, ai_analysis=ai_analysis, forward_status=forward_status, is_duplicate=1, duplicate_of=row[0], duplicate_count=row[1], beyond_window=1 if beyond_window else 0)
    session.add(dup); await session.flush()
    return SaveWebhookResult(dup.id, True, row[0], beyond_window)


async def save_webhook_data(
    data: WebhookData,
    source: str = "unknown",
    raw_payload: bytes | None = None,
    headers: HeadersDict | None = None,
    client_ip: str | None = None,
    ai_analysis: AnalysisResult | None = None,
    forward_status: str = "pending",
    alert_hash: str | None = None,
    is_duplicate: bool | None = None,
    original_event: WebhookEvent | None = None,
    beyond_window: bool = False,
    reanalyzed: bool = False,
    event_id: int | None = None,
) -> SaveWebhookResult:
    if alert_hash is None: alert_hash = generate_alert_hash(data, source)
    try:
        async with session_scope() as session:
            if is_duplicate is None:
                check = await check_duplicate_alert(alert_hash, session=session)
                is_duplicate, original_event, beyond_window = check.is_duplicate, check.original_event, check.beyond_window
            if is_duplicate and original_event:
                saved = await _save_duplicate_event(session, source=source, client_ip=client_ip, raw_payload=raw_payload, headers=headers, data=data, alert_hash=alert_hash, ai_analysis=ai_analysis, forward_status=forward_status, original_event=original_event, beyond_window=beyond_window, reanalyzed=reanalyzed, event_id=event_id)
                if saved: return saved
            if event_id is not None: return await _update_existing_event(session, event_id=event_id, source=source, client_ip=client_ip, raw_payload=raw_payload, headers=headers, data=data, alert_hash=alert_hash, ai_analysis=ai_analysis, forward_status=forward_status)
            return await _upsert_new_event(session, source=source, client_ip=client_ip, raw_payload=raw_payload, headers=headers, data=data, alert_hash=alert_hash, ai_analysis=ai_analysis, forward_status=forward_status, beyond_window=beyond_window)
    except Exception as e:
        logger.error(f"保存失败: {e}")
        file_id = await asyncio.to_thread(save_webhook_to_file, data, source, raw_payload, headers, client_ip, ai_analysis)
        return SaveWebhookResult(file_id, False, None, False)
