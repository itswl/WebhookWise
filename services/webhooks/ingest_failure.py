"""Durable failure recording for raw webhook ingestion."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy
from sqlalchemy.exc import IntegrityError

from core import json
from core.datetime_utils import parse_utc_datetime, utcnow
from core.logger import get_logger
from core.sensitive_data import redact_headers
from db.session import session_scope
from models import WebhookEvent, WebhookEventInput
from services.webhooks.types import WebhookProcessingStatus

logger = get_logger("webhooks.ingest_failure")


def _safe_error_message(err: Exception) -> str:
    return str(err)[:2000]


def _parse_raw_body(raw_body: str) -> dict[str, object] | None:
    try:
        loaded = json.loads(raw_body)
    except (TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _parse_received_at(value: str | None) -> datetime | None:
    return parse_utc_datetime(value)


async def record_raw_ingest_dead_letter(
    *,
    source: str,
    raw_headers: dict[str, str],
    raw_body: str,
    client_ip: str,
    request_id: str | None,
    received_at: str | None,
    retry_count: int,
    retryable: bool,
    err: Exception,
) -> int | None:
    """Persist a terminal raw-ingest failure as a dead-letter event."""
    if request_id:
        existing_id = await _update_existing_dead_letter(
            request_id=request_id,
            retry_count=retry_count,
            retryable=retryable,
            err=err,
        )
        if existing_id is not None:
            return existing_id

    event = WebhookEvent()
    event.fill_fields(
        WebhookEventInput(
            source=source or "unknown",
            request_id=request_id,
            client_ip=client_ip or "",
            raw_payload=raw_body.encode("utf-8"),
            headers=redact_headers(raw_headers),
            parsed_data=_parse_raw_body(raw_body),
            processing_status=WebhookProcessingStatus.DEAD_LETTER,
            retry_count=max(0, int(retry_count)),
            failure_reason="retry_exhausted" if retryable else "fat_err",
            error_message=_safe_error_message(err),
            timestamp=_parse_received_at(received_at) or utcnow(),
        )
    )

    try:
        async with session_scope() as session:
            session.add(event)
            await session.flush()
            logger.error(
                "[WebhookIngestFailure] raw webhook moved to dead-letter event_id=%s request_id=%s retry=%s error=%s",
                event.id,
                request_id,
                retry_count,
                err,
            )
            return int(event.id)
    except IntegrityError:
        if request_id:
            return await _update_existing_dead_letter(
                request_id=request_id,
                retry_count=retry_count,
                retryable=retryable,
                err=err,
            )
        raise


async def _update_existing_dead_letter(
    *,
    request_id: str,
    retry_count: int,
    retryable: bool,
    err: Exception,
) -> int | None:
    async with session_scope() as session:
        stmt = (
            sqlalchemy.update(WebhookEvent)
            .where(WebhookEvent.request_id == request_id)
            .values(
                processing_status=WebhookProcessingStatus.DEAD_LETTER,
                retry_count=max(0, int(retry_count)),
                failure_reason="retry_exhausted" if retryable else "fat_err",
                error_message=_safe_error_message(err),
                next_retry_at=None,
                updated_at=utcnow(),
            )
            .returning(WebhookEvent.id)
        )
        row = (await session.execute(stmt)).first()
        if not row:
            return None
        event_id = int(row[0])
        logger.error(
            "[WebhookIngestFailure] raw webhook updated to dead-letter event_id=%s request_id=%s retry=%s error=%s",
            event_id,
            request_id,
            retry_count,
            err,
        )
        return event_id
