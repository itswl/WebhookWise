"""Webhook 命令服务：接收、保存与状态重放。"""

import asyncio
import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy
from fastapi import Request
from sqlalchemy import column, insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.compression import COMPRESS_THRESHOLD_BYTES, compress_payload
from core.logger import logger
from core.sensitive_data import redact_headers
from db.session import session_scope
from models import WebhookEvent
from services.webhooks.policies import ClientIPPolicy, WebhookSavePolicy
from services.webhooks.types import AnalysisResult, WebhookData, WebhookProcessingStatus

HeadersDict = dict[str, str]


@dataclass(frozen=True)
class SaveWebhookResult:
    webhook_id: int
    is_duplicate: bool
    original_id: int | None
    beyond_window: bool


def get_client_ip(request: Request, *, policy: ClientIPPolicy | None = None) -> str:
    """获取客户端 IP 地址。"""
    policy = policy or ClientIPPolicy.from_config()
    if _is_trusted_proxy(request.client.host if request.client else None, policy):
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for and (ip := _first_valid_header_ip(forwarded_for)):
            return ip
        real_ip = request.headers.get("x-real-ip")
        if real_ip and (ip := _first_valid_header_ip(real_ip)):
            return ip
    return request.client.host if request.client else "unknown"


def _first_valid_header_ip(value: str) -> str | None:
    for raw in value.split(","):
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def _is_trusted_proxy(client_host: str | None, policy: ClientIPPolicy | None = None) -> bool:
    policy = policy or ClientIPPolicy.from_config()
    if not client_host or not policy.trust_proxy_headers:
        return False
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return client_host in set(policy.trusted_proxy_cidrs)

    for item in policy.trusted_proxy_cidrs:
        if not item:
            continue
        try:
            if client_ip in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            if item == client_host:
                return True
    return False


def _resolve_analysis_for_duplicate(
    ai_analysis: AnalysisResult | None, original: WebhookEvent, reanalyzed: bool
) -> tuple[AnalysisResult, str | None]:
    if ai_analysis:
        final_analysis, final_importance = ai_analysis, ai_analysis.get("importance")
    elif original.ai_analysis:
        final_analysis, final_importance = original.ai_analysis, original.importance
    else:
        final_analysis, final_importance = {}, None

    if ai_analysis and reanalyzed and (not original.ai_analysis or not original.ai_analysis.get("summary")):
        logger.info("更新原始告警 ID=%d 的AI分析结果（之前缺失）", original.id)
        original.ai_analysis = ai_analysis
        original.importance = ai_analysis.get("importance")

    return final_analysis, final_importance


async def quick_receive_webhook(
    session: AsyncSession,
    source: str,
    raw_headers: dict[str, Any],
    raw_body: str | bytes,
    parsed_data: dict[str, Any] | None = None,
) -> int:
    """同步最小化写入：仅持久化原始数据。"""
    raw_text = raw_body if isinstance(raw_body, str) else raw_body.decode("utf-8", errors="replace")
    body_len = len(raw_body) if isinstance(raw_body, bytes) else len(raw_body.encode("utf-8"))
    if body_len <= COMPRESS_THRESHOLD_BYTES:
        compressed = compress_payload(raw_text)
    else:
        compressed = await asyncio.to_thread(compress_payload, raw_text)

    stmt = (
        insert(WebhookEvent)
        .values(
            source=source,
            headers=redact_headers(raw_headers),
            raw_payload=compressed,
            parsed_data=parsed_data,
            processing_status=WebhookProcessingStatus.RECEIVED,
        )
        .returning(WebhookEvent.id)
    )
    res = await session.execute(stmt)
    return res.scalar_one()


async def replay_dead_letter(session: AsyncSession, event_id: int) -> bool:
    stmt = (
        update(WebhookEvent)
        .where(WebhookEvent.id == event_id, WebhookEvent.processing_status == WebhookProcessingStatus.DEAD_LETTER)
        .values(processing_status=WebhookProcessingStatus.RECEIVED, retry_count=0)
        .returning(WebhookEvent.id)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none() is not None


async def requeue_stuck_event(session: AsyncSession, event_id: int) -> bool:
    stmt = (
        update(WebhookEvent)
        .where(
            WebhookEvent.id == event_id,
            WebhookEvent.processing_status.in_(
                [WebhookProcessingStatus.RECEIVED, WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.FAILED]
            ),
        )
        .values(processing_status=WebhookProcessingStatus.RECEIVED)
    )
    res = await session.execute(stmt)
    return bool(res.rowcount)


async def mark_webhook_suppressed(
    *,
    event_id: int,
    data: WebhookData,
    source: str,
    raw_payload: bytes | None,
    headers: HeadersDict | None,
    client_ip: str | None,
    ai_analysis: AnalysisResult,
    alert_hash: str,
) -> None:
    """Persist a storm-suppressed event without running duplicate queries."""
    safe_headers = redact_headers(headers)
    async with session_scope() as session:
        event = await session.get(WebhookEvent, event_id)
        if not event:
            return
        event.fill_fields(
            source=source,
            client_ip=client_ip,
            parsed_data=data,
            alert_hash=alert_hash,
            ai_analysis=ai_analysis,
            importance=ai_analysis.get("importance") if ai_analysis else None,
            forward_status="skipped",
            is_duplicate=True,
            duplicate_of=None,
            duplicate_count=1,
            beyond_window=False,
            headers=safe_headers,
            raw_payload=raw_payload,
            processing_status=WebhookProcessingStatus.COMPLETED,
            next_retry_at=None,
        )
        await session.flush()


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
    if not original:
        return None
    original.duplicate_count = (original.duplicate_count or 1) + 1
    original.updated_at = datetime.now()
    final_ai_analysis, final_importance = _resolve_analysis_for_duplicate(ai_analysis, original, reanalyzed)

    if event_id is not None:
        dup_event = await session.get(WebhookEvent, event_id)
        if dup_event:
            dup_event.fill_fields(
                source=source,
                client_ip=client_ip,
                parsed_data=data,
                alert_hash=alert_hash,
                ai_analysis=final_ai_analysis,
                importance=final_importance,
                forward_status=forward_status,
                is_duplicate=True,
                duplicate_of=original.id,
                duplicate_count=original.duplicate_count,
                beyond_window=beyond_window,
                headers=headers,
                raw_payload=raw_payload,
                processing_status=WebhookProcessingStatus.COMPLETED,
                next_retry_at=None,
            )
            await session.flush()
            return SaveWebhookResult(dup_event.id, True, original.id, beyond_window)

    duplicate_event = WebhookEvent()
    duplicate_event.fill_fields(
        source=source,
        client_ip=client_ip,
        parsed_data=data,
        alert_hash=alert_hash,
        ai_analysis=final_ai_analysis,
        importance=final_importance,
        forward_status=forward_status,
        is_duplicate=True,
        duplicate_of=original.id,
        duplicate_count=original.duplicate_count,
        beyond_window=beyond_window,
        raw_payload=raw_payload,
        headers=headers,
        processing_status=WebhookProcessingStatus.COMPLETED,
        next_retry_at=None,
    )
    session.add(duplicate_event)
    await session.flush()
    return SaveWebhookResult(duplicate_event.id, True, original.id, beyond_window)


async def _save_new_event(session: AsyncSession, **kwargs: object) -> SaveWebhookResult:
    event = WebhookEvent()
    event.fill_fields(**kwargs, is_duplicate=False, duplicate_count=1, beyond_window=False, last_notified_at=None)
    session.add(event)
    await session.flush()
    return SaveWebhookResult(event.id, False, None, False)


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
    if not event:
        return await _save_new_event(
            session,
            source=source,
            client_ip=client_ip,
            raw_payload=raw_payload,
            headers=headers,
            data=data,
            alert_hash=alert_hash,
            ai_analysis=ai_analysis,
            forward_status=forward_status,
        )
    event.fill_fields(
        source=source,
        client_ip=client_ip,
        parsed_data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=ai_analysis.get("importance") if ai_analysis else None,
        forward_status=forward_status,
        is_duplicate=False,
        duplicate_of=None,
        duplicate_count=1,
        beyond_window=False,
        last_notified_at=None,
        headers=headers,
        raw_payload=raw_payload,
        processing_status=WebhookProcessingStatus.COMPLETED,
        next_retry_at=None,
    )
    try:
        async with session.begin_nested():
            await session.flush()
        return SaveWebhookResult(event.id, False, None, False)
    except sqlalchemy.exc.IntegrityError as e:
        if "idx_unique_alert_hash_original" in str(e):
            session.expunge(event)

            stmt = sqlalchemy.select(WebhookEvent).filter(
                WebhookEvent.alert_hash == alert_hash, WebhookEvent.is_duplicate.is_(False)
            )
            original = (await session.execute(stmt)).scalar_one_or_none()

            if original:
                event = await session.get(WebhookEvent, event_id)
                if event is None:
                    raise RuntimeError("WebhookEvent not found") from e
                original.duplicate_count = (original.duplicate_count or 1) + 1
                original.updated_at = datetime.now()

                event.fill_fields(
                    source=source,
                    client_ip=client_ip,
                    parsed_data=data,
                    alert_hash=alert_hash,
                    ai_analysis=ai_analysis,
                    importance=ai_analysis.get("importance") if ai_analysis else None,
                    forward_status=forward_status,
                    is_duplicate=True,
                    duplicate_of=original.id,
                    duplicate_count=original.duplicate_count,
                    beyond_window=False,
                    headers=headers,
                    raw_payload=raw_payload,
                    processing_status=WebhookProcessingStatus.COMPLETED,
                    next_retry_at=None,
                )
                await session.flush()
                return SaveWebhookResult(event.id, True, original.id, False)
        raise


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
    stmt: Any = (
        pg_insert(WebhookEvent)
        .values(
            source=source,
            client_ip=client_ip,
            timestamp=now,
            raw_payload=raw_payload,
            headers=dict(headers) if headers else {},
            parsed_data=data,
            alert_hash=alert_hash,
            ai_analysis=ai_analysis,
            importance=ai_analysis.get("importance") if ai_analysis else None,
            processing_status=WebhookProcessingStatus.COMPLETED,
            next_retry_at=None,
            forward_status=forward_status,
            is_duplicate=False,
            duplicate_count=1,
            beyond_window=False,
            last_notified_at=None,
        )
        .on_conflict_do_update(
            index_elements=["alert_hash"],
            index_where=(WebhookEvent.is_duplicate.is_(False)),
            set_={"duplicate_count": WebhookEvent.duplicate_count + 1, "updated_at": now},
        )
        .returning(WebhookEvent.id, WebhookEvent.duplicate_count, column("xmax"))
    )

    res = await session.execute(stmt)
    row = res.one()
    if row[2] == 0:
        return SaveWebhookResult(row[0], False, None, False)

    dup = WebhookEvent()
    dup.fill_fields(
        source=source,
        client_ip=client_ip,
        raw_payload=raw_payload,
        headers=headers,
        parsed_data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=ai_analysis.get("importance") if ai_analysis else None,
        forward_status=forward_status,
        is_duplicate=True,
        duplicate_of=row[0],
        duplicate_count=row[1],
        beyond_window=beyond_window,
        processing_status=WebhookProcessingStatus.COMPLETED,
        next_retry_at=None,
    )
    session.add(dup)
    await session.flush()
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
    policy: WebhookSavePolicy | None = None,
) -> SaveWebhookResult:
    if alert_hash is None:
        alert_hash = WebhookEvent.generate_hash(data, source)
    try:
        async with session_scope() as session:
            return await save_webhook_data_in_session(
                session,
                data=data,
                source=source,
                raw_payload=raw_payload,
                headers=headers,
                client_ip=client_ip,
                ai_analysis=ai_analysis,
                forward_status=forward_status,
                alert_hash=alert_hash,
                is_duplicate=is_duplicate,
                original_event=original_event,
                beyond_window=beyond_window,
                reanalyzed=reanalyzed,
                event_id=event_id,
                policy=policy,
            )
    except Exception:
        logger.exception("保存 webhook 事件失败")
        raise


async def save_webhook_data_in_session(
    session: AsyncSession,
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
    policy: WebhookSavePolicy | None = None,
) -> SaveWebhookResult:
    """Persist webhook data using an existing transaction/session."""
    policy = policy or WebhookSavePolicy.from_config()
    if alert_hash is None:
        alert_hash = WebhookEvent.generate_hash(data, source)
    safe_headers = redact_headers(headers)
    if is_duplicate is None:
        check = await WebhookEvent.check_duplicate(
            alert_hash, session=session, time_window_hours=policy.duplicate_window_hours
        )
        is_duplicate, original_event, beyond_window = (
            check.is_duplicate,
            check.original_event,
            check.beyond_window,
        )
    if is_duplicate and original_event:
        saved = await _save_duplicate_event(
            session,
            source=source,
            client_ip=client_ip,
            raw_payload=raw_payload,
            headers=safe_headers,
            data=data,
            alert_hash=alert_hash,
            ai_analysis=ai_analysis,
            forward_status=forward_status,
            original_event=original_event,
            beyond_window=beyond_window,
            reanalyzed=reanalyzed,
            event_id=event_id,
        )
        if saved:
            return saved
    if event_id is not None:
        return await _update_existing_event(
            session,
            event_id=event_id,
            source=source,
            client_ip=client_ip,
            raw_payload=raw_payload,
            headers=safe_headers,
            data=data,
            alert_hash=alert_hash,
            ai_analysis=ai_analysis,
            forward_status=forward_status,
        )
    return await _upsert_new_event(
        session,
        source=source,
        client_ip=client_ip,
        raw_payload=raw_payload,
        headers=safe_headers,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        forward_status=forward_status,
        beyond_window=beyond_window,
    )
