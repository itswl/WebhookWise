"""Webhook 命令服务：接收、保存与状态重放。"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

import sqlalchemy
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.compression import compress_payload
from core.logger import get_logger
from core.sensitive_data import redact_headers
from db.session import session_scope
from core.datetime_utils import utcnow
from models import WebhookEvent
from services.dedup import generate_alert_hash
from services.webhooks.repository import check_duplicate_event
from services.webhooks.types import AnalysisResult, WebhookData, WebhookProcessingStatus

logger = get_logger("webhooks.command_service")

HeadersDict = dict[str, str]


@dataclass(frozen=True)
class SaveWebhookResult:
    webhook_id: int
    is_duplicate: bool
    original_id: int | None


@dataclass(frozen=True, slots=True)
class SaveWebhookInput:
    data: WebhookData
    source: str = "unknown"
    raw_payload: bytes | None = None
    headers: HeadersDict | None = None
    client_ip: str | None = None
    request_id: str | None = None
    ai_analysis: AnalysisResult | None = None
    forward_status: str = "pending"
    alert_hash: str | None = None
    dedup_key: str | None = None
    is_duplicate: bool | None = None
    original_event: WebhookEvent | None = None
    original_event_id: int | None = None
    reanalyzed: bool = False
    skip_duplicate_lookup: bool = False
    prev_alert_id: int | None = None


@dataclass(frozen=True, slots=True)
class _DuplicateStatus:
    is_duplicate: bool
    original_event: WebhookEvent | None
    original_event_id: int | None


@dataclass(frozen=True, slots=True)
class _RequestIdResolution:
    existing_event_id: int | None
    skip_duplicate_lookup: bool
    completed_result: SaveWebhookResult | None = None



def _resolve_analysis_for_duplicate(
    ai_analysis: AnalysisResult | None, original: WebhookEvent, reanalyzed: bool
) -> tuple[AnalysisResult, str | None]:
    if ai_analysis:
        final_analysis, final_importance = ai_analysis, ai_analysis.get("importance")
    elif original.ai_analysis:
        final_analysis, final_importance = cast(AnalysisResult, original.ai_analysis), original.importance
    else:
        final_analysis, final_importance = {}, None

    if ai_analysis and reanalyzed and (not original.ai_analysis or not original.ai_analysis.get("summary")):
        logger.info("更新原始告警 ID=%d 的AI分析结果（之前缺失）", original.id)
        original.ai_analysis = dict(ai_analysis)
        original.importance = ai_analysis.get("importance")

    return final_analysis, final_importance


def _stored_raw_payload(raw_payload: bytes | None) -> bytes | None:
    if raw_payload is None:
        return None
    try:
        return compress_payload(raw_payload.decode("utf-8"))
    except Exception:
        return raw_payload


def _fill_duplicate_event(
    event: WebhookEvent,
    *,
    payload: SaveWebhookInput,
    original_id: int,
    duplicate_count: int,
    ai_analysis: AnalysisResult,
    importance: str | None,
) -> None:
    event.fill_fields(
        source=payload.source,
        request_id=payload.request_id,
        client_ip=payload.client_ip,
        parsed_data=payload.data,
        alert_hash=payload.alert_hash,
        dedup_key=payload.dedup_key,
        ai_analysis=ai_analysis,
        importance=importance,
        forward_status=payload.forward_status,
        is_duplicate=True,
        duplicate_of=original_id,
        duplicate_count=duplicate_count,
        headers=payload.headers,
        raw_payload=payload.raw_payload,
        processing_status=WebhookProcessingStatus.COMPLETED,
        next_retry_at=None,
    )


def _fill_completed_event(
    event: WebhookEvent,
    *,
    payload: SaveWebhookInput,
    processing_status: str = WebhookProcessingStatus.COMPLETED,
    next_retry_at: datetime | None = None,
) -> None:
    event.fill_fields(
        source=payload.source,
        request_id=payload.request_id,
        client_ip=payload.client_ip,
        raw_payload=payload.raw_payload,
        headers=payload.headers,
        parsed_data=payload.data,
        alert_hash=payload.alert_hash,
        dedup_key=payload.dedup_key,
        ai_analysis=payload.ai_analysis,
        importance=payload.ai_analysis.get("importance") if payload.ai_analysis else None,
        forward_status=payload.forward_status,
        processing_status=processing_status,
        next_retry_at=next_retry_at,
        is_duplicate=False,
        duplicate_count=1,
        last_notified_at=None,
        prev_alert_id=payload.prev_alert_id,
    )


async def _save_duplicate_event(
    session: AsyncSession,
    *,
    payload: SaveWebhookInput,
    duplicate_status: _DuplicateStatus,
    reanalyzed: bool,
    existing_event_id: int | None = None,
) -> SaveWebhookResult | None:
    original_event = duplicate_status.original_event
    original_event_id = duplicate_status.original_event_id
    original_id = original_event.id if original_event else original_event_id
    if original_id is None:
        return None

    original = await session.get(WebhookEvent, original_id)
    if original:
        original.duplicate_count = (original.duplicate_count or 1) + 1
        original.updated_at = utcnow()
        duplicate_count = original.duplicate_count
        final_ai_analysis, final_importance = _resolve_analysis_for_duplicate(payload.ai_analysis, original, reanalyzed)
    else:
        res = await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == original_id)
            .values(duplicate_count=WebhookEvent.duplicate_count + 1, updated_at=utcnow())
            .returning(WebhookEvent.id)
        )
        if res.scalar_one_or_none() is None:
            return None
        duplicate_count = 1
        final_ai_analysis = payload.ai_analysis or {}
        final_importance = final_ai_analysis.get("importance") if final_ai_analysis else None

    if existing_event_id is not None:
        dup_event = await session.get(WebhookEvent, existing_event_id)
        if dup_event:
            _fill_duplicate_event(
                dup_event,
                payload=payload,
                original_id=original_id,
                duplicate_count=duplicate_count,
                ai_analysis=final_ai_analysis,
                importance=final_importance,
            )
            await session.flush()
            return SaveWebhookResult(dup_event.id, True, original_id)

    duplicate_event = WebhookEvent()
    _fill_duplicate_event(
        duplicate_event,
        payload=payload,
        original_id=original_id,
        duplicate_count=duplicate_count,
        ai_analysis=final_ai_analysis,
        importance=final_importance,
    )
    session.add(duplicate_event)
    await session.flush()
    return SaveWebhookResult(duplicate_event.id, True, original_id)


async def _save_new_event(
    session: AsyncSession,
    *,
    payload: SaveWebhookInput,
    processing_status: str = WebhookProcessingStatus.COMPLETED,
    next_retry_at: datetime | None = None,
) -> SaveWebhookResult:
    event = WebhookEvent()
    _fill_completed_event(event, payload=payload, processing_status=processing_status, next_retry_at=next_retry_at)
    session.add(event)
    await session.flush()
    return SaveWebhookResult(event.id, False, None)


async def _update_existing_event(
    session: AsyncSession,
    *,
    event_id: int,
    payload: SaveWebhookInput,
) -> SaveWebhookResult:
    event = await session.get(WebhookEvent, event_id)
    if not event:
        return await _save_new_event(session, payload=payload)
    _fill_completed_event(event, payload=payload)
    try:
        async with session.begin_nested():
            await session.flush()
        return SaveWebhookResult(event.id, False, None)
    except sqlalchemy.exc.IntegrityError as e:
        if "idx_unique_alert_hash_original" in str(e):
            session.expunge(event)

            stmt = sqlalchemy.select(WebhookEvent).filter(
                WebhookEvent.alert_hash == payload.alert_hash, WebhookEvent.is_duplicate.is_(False)
            )
            original = (await session.execute(stmt)).scalar_one_or_none()

            if original:
                event = await session.get(WebhookEvent, event_id)
                if event is None:
                    raise RuntimeError("WebhookEvent not found") from e
                original.duplicate_count = (original.duplicate_count or 1) + 1
                original.updated_at = utcnow()

                _fill_duplicate_event(
                    event,
                    payload=payload,
                    original_id=original.id,
                    duplicate_count=original.duplicate_count or 1,
                    ai_analysis=payload.ai_analysis or {},
                    importance=payload.ai_analysis.get("importance") if payload.ai_analysis else None,
                )
                await session.flush()
                return SaveWebhookResult(event.id, True, original.id)
        raise


async def _resolve_request_id(
    session: AsyncSession,
    *,
    request_id: str | None,
    skip_duplicate_lookup: bool,
) -> _RequestIdResolution:
    if not request_id:
        return _RequestIdResolution(existing_event_id=None, skip_duplicate_lookup=skip_duplicate_lookup)

    existing = (
        await session.execute(sqlalchemy.select(WebhookEvent).where(WebhookEvent.request_id == request_id))
    ).scalar_one_or_none()
    if existing is None:
        return _RequestIdResolution(existing_event_id=None, skip_duplicate_lookup=skip_duplicate_lookup)

    if existing.processing_status == WebhookProcessingStatus.COMPLETED:
        logger.info(
            "[WebhookSave] request_id 已完成，跳过重复保存 request_id=%s event_id=%s",
            request_id,
            existing.id,
        )
        return _RequestIdResolution(
            existing_event_id=existing.id,
            skip_duplicate_lookup=True,
            completed_result=SaveWebhookResult(
                existing.id,
                bool(existing.is_duplicate),
                existing.duplicate_of,
            ),
        )

    logger.info(
        "[WebhookSave] request_id 已存在，复用事件继续保存 request_id=%s event_id=%s status=%s",
        request_id,
        existing.id,
        existing.processing_status,
    )
    return _RequestIdResolution(existing_event_id=existing.id, skip_duplicate_lookup=True)


async def _resolve_duplicate_status(
    session: AsyncSession,
    *,
    payload: SaveWebhookInput,
    is_duplicate: bool | None,
    original_event: WebhookEvent | None,
    original_event_id: int | None,
    skip_duplicate_lookup: bool,
) -> _DuplicateStatus:
    if is_duplicate is not None or skip_duplicate_lookup:
        return _DuplicateStatus(bool(is_duplicate), original_event, original_event_id)

    check = await check_duplicate_event(
        payload.alert_hash,
        session=session,
        time_window_hours=max(1, int(get_config_manager().retry.DEDUP_WINDOW_SECONDS) // 3600),
    )
    return _DuplicateStatus(
        check.is_duplicate,
        check.original_event,
        check.original_event.id if check.original_event else None,
    )


async def save_webhook_data(*, input: SaveWebhookInput) -> SaveWebhookResult:
    if input.alert_hash is None:
        object.__setattr__(input, "alert_hash", generate_alert_hash(input.data, input.source))
    try:
        async with session_scope() as session:
            return await save_webhook_data_in_session(session, input=input)
    except Exception:
        logger.exception("保存 webhook 事件失败")
        raise


async def save_webhook_data_in_session(
    session: AsyncSession, *, input: SaveWebhookInput
) -> SaveWebhookResult:
    """Persist webhook data using an existing transaction/session."""
    if input.alert_hash is None:
        object.__setattr__(input, "alert_hash", generate_alert_hash(input.data, input.source))
    payload = SaveWebhookInput(
        data=input.data,
        source=input.source,
        raw_payload=_stored_raw_payload(input.raw_payload),
        headers=redact_headers(input.headers),
        client_ip=input.client_ip,
        request_id=input.request_id,
        ai_analysis=input.ai_analysis,
        forward_status=input.forward_status,
        alert_hash=input.alert_hash or generate_alert_hash(input.data, input.source),
        dedup_key=input.dedup_key,
        prev_alert_id=input.prev_alert_id,
    )
    request_resolution = await _resolve_request_id(
        session,
        request_id=input.request_id,
        skip_duplicate_lookup=input.skip_duplicate_lookup,
    )
    if request_resolution.completed_result is not None:
        return request_resolution.completed_result

    existing_event_id = request_resolution.existing_event_id
    duplicate_status = await _resolve_duplicate_status(
        session,
        payload=payload,
        is_duplicate=input.is_duplicate,
        original_event=input.original_event,
        original_event_id=input.original_event_id,
        skip_duplicate_lookup=request_resolution.skip_duplicate_lookup,
    )
    if duplicate_status.is_duplicate and (duplicate_status.original_event or duplicate_status.original_event_id):
        saved = await _save_duplicate_event(
            session,
            payload=payload,
            duplicate_status=duplicate_status,
            reanalyzed=input.reanalyzed,
            existing_event_id=existing_event_id,
        )
        if saved:
            return saved
    if existing_event_id is not None:
        return await _update_existing_event(
            session,
            event_id=existing_event_id,
            payload=payload,
        )
    return await _save_new_event(
        session,
        payload=payload,
        processing_status=WebhookProcessingStatus.COMPLETED,
        next_retry_at=None,
    )
