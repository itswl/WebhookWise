import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import orjson
from fastapi import Request
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.compression import COMPRESS_THRESHOLD_BYTES, compress_payload
from core.config import Config
from core.logger import logger

# We will import the purely utility functions back from core.utils
from core.utils import generate_alert_hash  # noqa: F401
from db.session import session_scope
from models import FailedForward, SystemConfig, WebhookEvent
from services.file_backup import get_webhooks_from_files, save_webhook_to_file

WebhookData = dict[str, Any]

HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]


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
) -> WebhookEvent:
    """同步最小化写入：仅持久化原始数据，不做任何分析/转发"""
    raw_text = raw_body if isinstance(raw_body, str) else raw_body.decode("utf-8", errors="replace")
    # 大 payload 在线程中压缩，避免阻塞事件循环
    body_len = len(raw_body) if isinstance(raw_body, bytes) else len(raw_body.encode("utf-8"))
    if body_len > COMPRESS_THRESHOLD_BYTES:
        compressed = await asyncio.to_thread(compress_payload, raw_text)
    else:
        compressed = compress_payload(raw_text)
    event = WebhookEvent(
        source=source,
        headers=raw_headers if isinstance(raw_headers, dict) else orjson.loads(raw_headers),
        raw_payload=compressed,
        parsed_data=parsed_data,
        processing_status="received",
    )
    session.add(event)
    await session.flush()  # 获取 ID 但不 commit（让调用方控制事务）
    return event


async def _query_last_beyond_window_event(session: AsyncSession, alert_hash: str) -> WebhookEvent | None:
    stmt = (
        select(WebhookEvent)
        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.beyond_window == 1)
        .order_by(WebhookEvent.timestamp.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def _query_latest_original_event(session: AsyncSession, alert_hash: str) -> WebhookEvent | None:
    stmt = (
        select(WebhookEvent)
        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.is_duplicate == 0)
        .order_by(WebhookEvent.timestamp.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().first()


def _decode_raw_payload(raw_payload: bytes | None) -> bytes | None:
    """将原始 bytes payload 压缩为 gzip bytes。"""
    if not raw_payload:
        return None
    return compress_payload(raw_payload.decode("utf-8"))


def _normalize_headers(headers: HeadersDict | None) -> HeadersDict:
    return dict(headers) if headers else {}


def _fill_event_fields(
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
        event.headers = _normalize_headers(headers)
    if raw_payload is not None:
        event.raw_payload = _decode_raw_payload(raw_payload)


def _build_event(
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
    _fill_event_fields(
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
    """UPDATE 已由 quick_receive_webhook 创建的记录，补全所有分析字段。"""
    event = await session.get(WebhookEvent, event_id)
    if not event:
        # 极端情况：记录被删，降级为 INSERT
        logger.warning(f"[save] event_id={event_id} 不存在，降级为新建")
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

    # 补全字段（raw_payload / headers 已在 quick_receive_webhook 写入，仅在需要时覆盖）
    _fill_event_fields(
        event,
        source=source,
        client_ip=client_ip,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=ai_analysis.get("importance") if ai_analysis else None,
        forward_status=forward_status,
        is_duplicate=0,
        duplicate_of=None,
        duplicate_count=1,
        beyond_window=0,
        last_notified_at=datetime.now(),
        headers=headers,
        raw_payload=raw_payload,
    )

    await session.flush()
    logger.info(f"[save] UPDATE 已有记录: ID={event.id}, alert_hash={alert_hash}")

    if Config.server.ENABLE_FILE_BACKUP:
        save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)

    return SaveWebhookResult(event.id, False, None, False)


async def _save_new_event(
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
) -> SaveWebhookResult:
    webhook_event = _build_event(
        source=source,
        client_ip=client_ip,
        raw_payload=raw_payload,
        headers=headers,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=ai_analysis.get("importance") if ai_analysis else None,
        forward_status=forward_status,
        is_duplicate=0,
        duplicate_of=None,
        duplicate_count=1,
        beyond_window=0,
        last_notified_at=datetime.now(),
    )

    session.add(webhook_event)
    await session.flush()
    logger.info(f"Webhook 数据已保存到数据库: ID={webhook_event.id}")

    if Config.server.ENABLE_FILE_BACKUP:
        save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)

    return SaveWebhookResult(webhook_event.id, False, None, False)


def get_client_ip(request: Request) -> str:
    """获取客户端 IP 地址"""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip

    return request.client.host if request.client else "unknown"


async def get_all_webhooks(
    page: int = 1, page_size: int = 20, cursor_id: int | None = None, fields: str = "summary"
) -> tuple[list[dict], int, int | None]:
    """
    从数据库获取 webhook 数据（纯 Keyset 游标分页）

    Args:
        page: Deprecated, 保留向后兼容但不影响查询
        page_size: 每页数量
        cursor_id: 游标 ID，获取此 ID 之前的数据（按 ID 降序）
        fields: 字段选择 - 'summary'(摘要), 'full'(完整)

    Returns:
        tuple: (webhook数据列表, 总数量(始终为-1), 下一页游标ID)
    """
    try:
        async with session_scope() as session:
            # 构建查询（纯 Keyset，不使用 OFFSET）
            query = select(WebhookEvent)

            if cursor_id is not None:
                query = query.filter(WebhookEvent.id < cursor_id)

            query = query.order_by(WebhookEvent.id.desc()).limit(page_size + 1)
            result = await session.execute(query)
            events = list(result.scalars().all())

            # page_size+1 策略判断 has_more
            has_more = len(events) > page_size
            if has_more:
                events = events[:page_size]

            # 根据 fields 参数决定返回哪些字段
            if fields == "summary":
                # 摘要模式：只返回列表必需的字段，减少数据传输量
                webhooks = [event.to_summary_dict() for event in events]
            else:
                # 完整模式：返回所有字段
                webhooks = [event.to_dict() for event in events]

            # 为重复告警添加窗口信息和上次告警 ID（批量计算优化）
            # 直接从数据库字段读取，无需动态计算
            for webhook in webhooks:
                # beyond_window 已经在数据库中固化，直接使用
                beyond_window = bool(webhook.get("beyond_window", 0))
                webhook["beyond_time_window"] = beyond_window
                webhook["is_within_window"] = not beyond_window if webhook.get("is_duplicate") else False

            # 批量计算上次告警 ID（使用 LAG 窗口函数优化）
            event_ids = [e.id for e in events]
            all_hashes = list({e.alert_hash for e in events if e.alert_hash})

            if all_hashes:
                try:
                    # 使用 LAG 窗口函数在数据库中直接计算 prev_alert_id
                    lag_id = (
                        func.lag(WebhookEvent.id)
                        .over(
                            partition_by=WebhookEvent.alert_hash,
                            order_by=WebhookEvent.timestamp,
                        )
                        .label("prev_alert_id")
                    )
                    lag_ts = (
                        func.lag(WebhookEvent.timestamp)
                        .over(
                            partition_by=WebhookEvent.alert_hash,
                            order_by=WebhookEvent.timestamp,
                        )
                        .label("prev_alert_timestamp")
                    )

                    # 子查询：对涉及的 alert_hash 计算窗口函数
                    subq = (
                        select(
                            WebhookEvent.id.label("event_id"),
                            lag_id,
                            lag_ts,
                        )
                        .filter(WebhookEvent.alert_hash.in_(all_hashes))
                        .subquery()
                    )

                    # 外层只取当前页的事件 ID
                    prev_query = select(
                        subq.c.event_id,
                        subq.c.prev_alert_id,
                        subq.c.prev_alert_timestamp,
                    ).filter(subq.c.event_id.in_(event_ids))

                    result = await session.execute(prev_query)
                    prev_map = {row.event_id: (row.prev_alert_id, row.prev_alert_timestamp) for row in result.all()}

                    # 填充到 webhook 结果中
                    for webhook in webhooks:
                        wid = webhook.get("id")
                        if wid and wid in prev_map:
                            prev_id, prev_ts = prev_map[wid]
                            webhook["prev_alert_id"] = prev_id
                            webhook["prev_alert_timestamp"] = prev_ts.isoformat() if prev_ts else None
                        else:
                            webhook["prev_alert_id"] = None
                            webhook["prev_alert_timestamp"] = None
                except Exception as e:
                    logger.warning(f"LAG 窗口函数计算 prev_alert_id 失败: {e}")
                    for webhook in webhooks:
                        webhook["prev_alert_id"] = None
                        webhook["prev_alert_timestamp"] = None
            else:
                # 没有 alert_hash 的设为 None
                for webhook in webhooks:
                    webhook["prev_alert_id"] = None
                    webhook["prev_alert_timestamp"] = None

            # 计算下一页游标
            next_cursor = events[-1].id if has_more and events else None

            return webhooks, -1, next_cursor

    except Exception as e:
        logger.error(f"从数据库查询 webhook 数据失败: {e!s}")
        webhooks = get_webhooks_from_files(limit=page_size)
        return webhooks, len(webhooks), None


# ── 转发失败重试补偿 CRUD ──


async def record_failed_forward(
    webhook_event_id: int,
    forward_rule_id: int | None,
    target_url: str,
    target_type: str,
    failure_reason: str,
    error_message: str | None = None,
    forward_data: dict | None = None,
    forward_headers: dict | None = None,
    max_retries: int | None = None,
    session: AsyncSession | None = None,
) -> FailedForward | None:
    """写入转发失败记录，计算首次重试时间"""
    if max_retries is None:
        max_retries = Config.retry.FORWARD_RETRY_MAX_RETRIES

    now = datetime.now()
    next_retry_at = now + timedelta(seconds=Config.retry.FORWARD_RETRY_INITIAL_DELAY)

    record = FailedForward(
        webhook_event_id=webhook_event_id,
        forward_rule_id=forward_rule_id,
        target_url=target_url,
        target_type=target_type,
        status="pending",
        failure_reason=failure_reason,
        error_message=error_message,
        retry_count=0,
        max_retries=max_retries,
        next_retry_at=next_retry_at,
        forward_data=forward_data,
        forward_headers=forward_headers,
        created_at=now,
        updated_at=now,
    )

    try:
        if session is not None:
            session.add(record)
            await session.flush()
            logger.info(f"转发失败记录已写入: ID={record.id}, target={target_url}")
            return record

        async with session_scope() as scoped_session:
            scoped_session.add(record)
            await scoped_session.flush()
            logger.info(f"转发失败记录已写入: ID={record.id}, target={target_url}")
            return record
    except Exception as e:
        logger.error(f"写入转发失败记录失败: {e!s}")
        return None


async def get_failed_forwards(
    status: str | None = None,
    target_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession | None = None,
) -> tuple[list[dict], int]:
    """按状态/类型分页查询转发失败记录"""

    async def _query(sess: AsyncSession) -> tuple[list[dict], int]:
        # 构建基础条件
        conditions = []
        if status:
            conditions.append(FailedForward.status == status)
        if target_type:
            conditions.append(FailedForward.target_type == target_type)

        # 总数查询
        count_stmt = select(func.count()).select_from(FailedForward)
        for cond in conditions:
            count_stmt = count_stmt.filter(cond)
        total = (await sess.execute(count_stmt)).scalar() or 0

        # 数据查询
        query = select(FailedForward)
        for cond in conditions:
            query = query.filter(cond)
        query = query.order_by(FailedForward.next_retry_at.asc()).offset(offset).limit(limit)
        result = await sess.execute(query)
        records = result.scalars().all()

        return [r.to_dict() for r in records], total

    try:
        if session is not None:
            return await _query(session)
        async with session_scope() as scoped_session:
            return await _query(scoped_session)
    except Exception as e:
        logger.error(f"查询转发失败记录失败: {e!s}")
        return [], 0


async def get_failed_forward_stats(
    session: AsyncSession | None = None,
) -> dict[str, int]:
    """统计各状态数量"""

    async def _query(sess: AsyncSession) -> dict[str, int]:
        stmt = select(FailedForward.status, func.count()).group_by(FailedForward.status)
        result = await sess.execute(stmt)
        rows = result.all()

        stats = {"pending": 0, "retrying": 0, "success": 0, "exhausted": 0, "total": 0}
        for status_val, count in rows:
            if status_val in stats:
                stats[status_val] = count
            stats["total"] += count
        return stats

    try:
        if session is not None:
            return await _query(session)
        async with session_scope() as scoped_session:
            return await _query(scoped_session)
    except Exception as e:
        logger.error(f"统计转发失败记录失败: {e!s}")
        return {"pending": 0, "retrying": 0, "success": 0, "exhausted": 0, "total": 0}


async def manual_retry_reset(
    failed_forward_id: int,
    session: AsyncSession | None = None,
) -> bool:
    """将 exhausted 记录重置为 pending，retry_count 归 0"""

    async def _reset(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record:
            logger.warning(f"转发失败记录不存在: ID={failed_forward_id}")
            return False
        if record.status != "exhausted":
            logger.warning(f"记录状态不是 exhausted，无法重置: ID={failed_forward_id}, status={record.status}")
            return False

        now = datetime.now()
        record.status = "pending"
        record.retry_count = 0
        record.next_retry_at = now + timedelta(seconds=Config.retry.FORWARD_RETRY_INITIAL_DELAY)
        record.updated_at = now
        await sess.flush()
        logger.info(f"转发失败记录已重置为 pending: ID={failed_forward_id}")
        return True

    try:
        if session is not None:
            return await _reset(session)
        async with session_scope() as scoped_session:
            return await _reset(scoped_session)
    except Exception as e:
        logger.error(f"重置转发失败记录失败: {e!s}")
        return False


async def delete_failed_forward(
    failed_forward_id: int,
    session: AsyncSession | None = None,
) -> bool:
    """删除转发失败记录"""

    async def _delete(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record:
            logger.warning(f"转发失败记录不存在: ID={failed_forward_id}")
            return False
        await sess.delete(record)
        await sess.flush()
        logger.info(f"转发失败记录已删除: ID={failed_forward_id}")
        return True

    try:
        if session is not None:
            return await _delete(session)
        async with session_scope() as scoped_session:
            return await _delete(scoped_session)
    except Exception as e:
        logger.error(f"删除转发失败记录失败: {e!s}")
        return False


# ── 运行时配置 CRUD ──


async def get_all_runtime_configs():
    """批量加载所有运行时配置"""
    async with session_scope() as session:
        result = await session.execute(select(SystemConfig))
        return {row.key: row for row in result.scalars().all()}


async def get_runtime_config(key: str):
    """读取单个配置"""
    async with session_scope() as session:
        result = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
        return result.scalar_one_or_none()


async def upsert_runtime_config(key: str, value: str, value_type: str = "str", updated_by: str = "api"):
    """写入或更新配置（upsert）"""
    async with session_scope() as session:
        existing = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
        config = existing.scalar_one_or_none()
        if config:
            config.value = value
            config.value_type = value_type
            config.updated_by = updated_by
        else:
            config = SystemConfig(key=key, value=value, value_type=value_type, updated_by=updated_by)
            session.add(config)
        await session.commit()
        return config


async def cleanup_old_success_records(
    days: int = 7,
    session: AsyncSession | None = None,
) -> int:
    """清理 N 天前已成功的记录，返回删除数量"""
    cutoff = datetime.now() - timedelta(days=days)

    async def _cleanup(sess: AsyncSession) -> int:
        stmt = (
            sa_delete(FailedForward).where(FailedForward.status == "success").where(FailedForward.updated_at < cutoff)
        )
        result = await sess.execute(stmt)
        count = result.rowcount
        await sess.flush()
        if count > 0:
            logger.info(f"已清理 {count} 条 {days} 天前的成功转发记录")
        return count

    try:
        if session is not None:
            return await _cleanup(session)
        async with session_scope() as scoped_session:
            return await _cleanup(scoped_session)
    except Exception as e:
        logger.error(f"清理成功转发记录失败: {e!s}")
        return 0
