"""业务协调层 — 从 crud/webhook.py 提取的保存协调逻辑。"""

from datetime import datetime
from typing import Any

from sqlalchemy import column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Config
from core.logger import logger
from core.utils import generate_alert_hash
from crud.webhook import (
    SaveWebhookResult,
    WebhookData,
    _build_event,
    _decode_raw_payload,
    _fill_event_fields,
    _normalize_headers,
    _update_existing_event,
)
from db.session import session_scope
from models import WebhookEvent
from services.dedup_strategy import _resolve_analysis_for_duplicate, check_duplicate_alert
from services.file_backup import save_webhook_to_file

HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]


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
    logger.info(f"发现重复告警，原始告警ID={original.id}, 已重复{original.duplicate_count}次")

    final_ai_analysis, final_importance = _resolve_analysis_for_duplicate(ai_analysis, original, reanalyzed)

    # 如果有 event_id，UPDATE 已有记录为重复告警，而非 INSERT 新记录
    if event_id is not None:
        dup_event = await session.get(WebhookEvent, event_id)
        if dup_event:
            _fill_event_fields(
                dup_event,
                source=source,
                client_ip=client_ip,
                data=data,
                alert_hash=alert_hash,
                ai_analysis=final_ai_analysis,
                importance=final_importance,
                forward_status=forward_status,
                is_duplicate=1,
                duplicate_of=original.id,
                duplicate_count=original.duplicate_count,
                beyond_window=1 if beyond_window else 0,
                headers=headers,
                raw_payload=raw_payload,
            )
            await session.flush()
            logger.info(f"[save] UPDATE 重复告警记录: ID={dup_event.id}, original={original.id}")
            if Config.server.ENABLE_FILE_BACKUP:
                save_webhook_to_file(data, source, raw_payload, headers, client_ip, final_ai_analysis)
            return SaveWebhookResult(dup_event.id, True, original.id, beyond_window)

    duplicate_event = _build_event(
        source=source,
        client_ip=client_ip,
        raw_payload=raw_payload,
        headers=headers,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=final_ai_analysis,
        importance=final_importance,
        forward_status=forward_status,
        is_duplicate=1,
        duplicate_of=original.id,
        duplicate_count=original.duplicate_count,
        beyond_window=1 if beyond_window else 0,
    )

    session.add(duplicate_event)
    await session.flush()

    if ai_analysis:
        logger.info(f"重复告警已保存: ID={duplicate_event.id}, 使用传入的AI分析结果")
    elif original.ai_analysis:
        logger.info(f"重复告警已保存: ID={duplicate_event.id}, 复用原始告警 {original.id} 的AI分析结果")
    else:
        logger.info(f"重复告警已保存: ID={duplicate_event.id}, 无AI分析结果")

    if Config.server.ENABLE_FILE_BACKUP:
        save_webhook_to_file(data, source, raw_payload, headers, client_ip, final_ai_analysis)

    return SaveWebhookResult(duplicate_event.id, True, original.id, beyond_window)


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
    """使用 INSERT ON CONFLICT DO UPDATE 原子性处理原始告警写入。

    利用部分唯一索引 idx_unique_alert_hash_original (alert_hash WHERE is_duplicate=0)
    处理并发冲突：
    - 无冲突：正常插入新原始告警
    - 冲突：原子性更新 duplicate_count 和 updated_at，通过 xmax 判断是否为冲突

    相比旧的 DO NOTHING + SELECT + INSERT 三步操作，消除了竞态窗口。
    """
    now = datetime.now()
    importance = ai_analysis.get("importance") if ai_analysis else None

    stmt = (
        pg_insert(WebhookEvent)
        .values(
            source=source,
            client_ip=client_ip,
            timestamp=now,
            raw_payload=_decode_raw_payload(raw_payload),
            headers=_normalize_headers(headers),
            parsed_data=data,
            alert_hash=alert_hash,
            ai_analysis=ai_analysis,
            importance=importance,
            processing_status="completed",
            forward_status=forward_status,
            is_duplicate=0,
            duplicate_of=None,
            duplicate_count=1,
            beyond_window=0,
            last_notified_at=now,
        )
        .on_conflict_do_update(
            index_elements=["alert_hash"],
            index_where=(WebhookEvent.is_duplicate == 0),
            set_={
                "duplicate_count": WebhookEvent.duplicate_count + 1,
                "updated_at": now,
            },
        )
        .returning(
            WebhookEvent.id,
            WebhookEvent.duplicate_count,
            # xmax == 0 → 新插入行；xmax > 0 → 已有行被更新（冲突）
            column("xmax"),
        )
    )

    result = await session.execute(stmt)
    row = result.one()  # RETURNING 始终返回一行（无论 INSERT 还是 UPDATE）
    row_id = row[0]
    dup_count = row[1]
    xmax = row[2]
    is_new = xmax == 0

    if is_new:
        # 插入成功，无冲突
        logger.info(f"Webhook 数据已保存到数据库 (UPSERT): ID={row_id}")
        if Config.server.ENABLE_FILE_BACKUP:
            save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)
        return SaveWebhookResult(row_id, False, None, False)

    # 冲突：已有原始告警，原子性 +1 duplicate_count 已完成
    logger.warning(
        f"UPSERT 冲突 (原子更新): alert_hash={alert_hash}, original_id={row_id}, duplicate_count={dup_count}"
    )

    # 写入重复告警记录
    final_ai_analysis = ai_analysis if ai_analysis else None
    final_importance = importance

    dup_event = _build_event(
        source=source,
        client_ip=client_ip,
        raw_payload=raw_payload,
        headers=headers,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=final_ai_analysis,
        importance=final_importance,
        forward_status=forward_status,
        is_duplicate=1,
        duplicate_of=row_id,
        duplicate_count=dup_count,
        beyond_window=1 if beyond_window else 0,
    )
    session.add(dup_event)
    await session.flush()

    logger.info(f"并发冲突降级：重复告警已保存 ID={dup_event.id}, original={row_id}")
    if Config.server.ENABLE_FILE_BACKUP:
        save_webhook_to_file(data, source, raw_payload, headers, client_ip, final_ai_analysis)

    return SaveWebhookResult(dup_event.id, True, row_id, beyond_window)


def _save_to_file_fallback(
    data: WebhookData,
    source: str,
    raw_payload: bytes | None,
    headers: HeadersDict | None,
    client_ip: str | None,
    ai_analysis: AnalysisResult | None,
) -> SaveWebhookResult:
    file_id = save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)
    return SaveWebhookResult(file_id, False, None, False)


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
    """保存 webhook 数据到数据库（使用 UPSERT 处理并发竞态）。

    当 event_id 有值时，UPDATE 已由 quick_receive_webhook 创建的记录，
    避免重复 INSERT（双写）。同时在同一事务中更新 processing_status。

    并发冲突通过 INSERT ON CONFLICT DO NOTHING 原子性处理，
    无需重试循环和指数退避。
    """
    if alert_hash is None:
        alert_hash = generate_alert_hash(data, source)

    try:
        async with session_scope() as session:
            # 在同一事务内重新判重，避免外层结果在高并发下过期。
            if is_duplicate is None:
                duplicate_check = await check_duplicate_alert(alert_hash, session=session)
                is_duplicate = duplicate_check.is_duplicate
                original_event = duplicate_check.original_event
                beyond_window = duplicate_check.beyond_window

            if is_duplicate and original_event:
                saved = await _save_duplicate_event(
                    session,
                    source=source,
                    client_ip=client_ip,
                    raw_payload=raw_payload,
                    headers=headers,
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

            # event_id 有值：UPDATE 已有记录而非 INSERT 新记录
            if event_id is not None:
                return await _update_existing_event(
                    session,
                    event_id=event_id,
                    source=source,
                    client_ip=client_ip,
                    raw_payload=raw_payload,
                    headers=headers,
                    data=data,
                    alert_hash=alert_hash,
                    ai_analysis=ai_analysis,
                    forward_status=forward_status,
                )

            # 使用 UPSERT（INSERT ON CONFLICT DO NOTHING）原子性处理并发写入
            return await _upsert_new_event(
                session,
                source=source,
                client_ip=client_ip,
                raw_payload=raw_payload,
                headers=headers,
                data=data,
                alert_hash=alert_hash,
                ai_analysis=ai_analysis,
                forward_status=forward_status,
                beyond_window=beyond_window,
            )

    except Exception as e:
        logger.error(f"保存 webhook 数据到数据库失败: {e!s}")
        return _save_to_file_fallback(data, source, raw_payload, headers, client_ip, ai_analysis)
