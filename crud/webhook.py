import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Config
from core.logger import logger

# We will import the purely utility functions back from core.utils
from core.utils import generate_alert_hash
from db.session import get_session, session_scope
from models import WebhookEvent

WebhookData = dict[str, Any]
HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]

@dataclass(frozen=True)
class DuplicateCheckResult:
    is_duplicate: bool
    original_event: WebhookEvent | None
    beyond_window: bool
    last_beyond_window_event: WebhookEvent | None


@dataclass(frozen=True)
class SaveWebhookResult:
    webhook_id: int | str
    is_duplicate: bool
    original_id: int | None
    beyond_window: bool



async def _query_last_beyond_window_event(session: AsyncSession, alert_hash: str) -> WebhookEvent | None:
    stmt = select(WebhookEvent).filter(
        WebhookEvent.alert_hash == alert_hash,
        WebhookEvent.beyond_window == 1
    ).order_by(WebhookEvent.timestamp.desc())
    result = await session.execute(stmt)
    return result.scalars().first()


async def _query_latest_original_event(session: AsyncSession, alert_hash: str) -> WebhookEvent | None:
    stmt = select(WebhookEvent).filter(
        WebhookEvent.alert_hash == alert_hash,
        WebhookEvent.is_duplicate == 0
    ).order_by(WebhookEvent.timestamp.desc())
    result = await session.execute(stmt)
    return result.scalars().first()


async def _find_recent_window_event(
    session: AsyncSession,
    alert_hash: str,
    time_threshold: datetime
) -> WebhookEvent | None:
    stmt = select(WebhookEvent).filter(
        WebhookEvent.alert_hash == alert_hash,
        WebhookEvent.timestamp >= time_threshold
    ).order_by(WebhookEvent.timestamp.desc())
    result = await session.execute(stmt)
    return result.scalars().first()


def _resolve_window_start(
    original_ref: WebhookEvent,
    last_beyond_window: WebhookEvent | None
) -> tuple[datetime, int]:
    if last_beyond_window:
        logger.debug(f"找到窗口外记录作为起点: ID={last_beyond_window.id}, 时间={last_beyond_window.timestamp}")
        return last_beyond_window.timestamp, last_beyond_window.id

    logger.debug(f"使用原始告警作为起点: ID={original_ref.id}, 时间={original_ref.timestamp}")
    return original_ref.timestamp, original_ref.id


async def _resolve_original_reference(session: AsyncSession, any_event: WebhookEvent) -> WebhookEvent:
    original_id = any_event.duplicate_of if any_event.is_duplicate else any_event.id
    if not original_id:
        return any_event
    ref = await session.get(WebhookEvent, original_id)
    return ref or any_event


async def check_duplicate_alert(
    alert_hash: str,
    time_window_hours: int | None = None,
    session: AsyncSession | None = None,
    check_beyond_window: bool = False
) -> DuplicateCheckResult:
    """
    检查是否存在重复告警

    Args:
        alert_hash: 告警哈希值
        time_window_hours: 时间窗口（小时）
        session: 数据库会话（如果提供，使用现有事务；否则创建新会话）
        check_beyond_window: 是否检查时间窗口外的历史告警

    Returns:
        DuplicateCheckResult
    """
    if not alert_hash:
        return DuplicateCheckResult(False, None, False, None)

    if time_window_hours is None:
        time_window_hours = Config.DUPLICATE_ALERT_TIME_WINDOW

    should_close = session is None
    if should_close:
        session = get_session()

    now = datetime.now()

    try:
        time_threshold = now - timedelta(hours=time_window_hours)

        # 先查窗口内最新记录，保证同一时间窗口内只产生一条“原始上下文”。
        # 这样在并发写入时，后续请求可以稳定复用同一条分析结果。
        any_event = await _find_recent_window_event(session, alert_hash, time_threshold)

        if any_event:
            original_ref = await _resolve_original_reference(session, any_event)
            original_id = original_ref.id
            last_beyond_window = await _query_last_beyond_window_event(session, alert_hash)

            # 窗口起点策略：优先 recent beyond_window，其次原始告警。
            window_start, window_start_id = _resolve_window_start(original_ref, last_beyond_window)

            time_diff_hours = (now - window_start).total_seconds() / 3600
            is_within_window = time_diff_hours <= time_window_hours

            if is_within_window:
                logger.info(
                    f"检测到窗口内重复: hash={alert_hash}, 最近记录ID={any_event.id}, "
                    f"原始告警ID={original_id}, 窗口起点ID={window_start_id}, "
                    f"距窗口起点={time_diff_hours:.1f}小时"
                )
                return DuplicateCheckResult(True, original_ref, False, last_beyond_window)

            logger.info(
                f"检测到窗口外重复: hash={alert_hash}, 最近记录ID={any_event.id}, "
                f"原始告警ID={original_id}, 窗口起点ID={window_start_id}, "
                f"距窗口起点={time_diff_hours:.1f}小时"
            )
            return DuplicateCheckResult(True, original_ref, True, last_beyond_window)

        if check_beyond_window:
            # 并发场景下，recent beyond_window 用于判断是否可直接复用他 worker 的结果。
            last_beyond_window = await _query_last_beyond_window_event(session, alert_hash)
            history_event = await _query_latest_original_event(session, alert_hash)

            if history_event:
                time_diff = (now - history_event.timestamp).total_seconds() / 3600
                logger.info(
                    f"窗口外发现历史告警: hash={alert_hash}, "
                    f"原始告警ID={history_event.id}, 时间差={time_diff:.1f}小时"
                )
                # 返回历史原始事件与 recent beyond_window，交给上层做“复用或重算”决策。
                return DuplicateCheckResult(False, history_event, True, last_beyond_window)

        return DuplicateCheckResult(False, None, False, None)

    except Exception as e:
        logger.error(f"检查重复告警失败: {e!s}")
        return DuplicateCheckResult(False, None, False, None)
    finally:
        if should_close:
            await session.close()




def _decode_raw_payload(raw_payload: bytes | None) -> str | None:
    return raw_payload.decode('utf-8') if raw_payload else None


def _normalize_headers(headers: HeadersDict | None) -> HeadersDict:
    return dict(headers) if headers else {}


def _resolve_analysis_for_duplicate(
    ai_analysis: AnalysisResult | None,
    original: WebhookEvent,
    reanalyzed: bool
) -> tuple[AnalysisResult, str | None]:
    if ai_analysis:
        final_analysis = ai_analysis
        final_importance = ai_analysis.get('importance')
    elif original.ai_analysis:
        final_analysis = original.ai_analysis
        final_importance = original.importance
    else:
        final_analysis = {}
        final_importance = None

    if ai_analysis and reanalyzed and (not original.ai_analysis or not original.ai_analysis.get('summary')):
        logger.info(f"更新原始告警 ID={original.id} 的AI分析结果（之前缺失）")
        original.ai_analysis = ai_analysis
        original.importance = ai_analysis.get('importance')

    return final_analysis, final_importance


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
    last_notified_at: datetime | None = None
) -> WebhookEvent:
    return WebhookEvent(
        source=source,
        client_ip=client_ip,
        timestamp=datetime.now(),
        raw_payload=_decode_raw_payload(raw_payload),
        headers=_normalize_headers(headers),
        parsed_data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=importance,
        forward_status=forward_status,
        is_duplicate=is_duplicate,
        duplicate_of=duplicate_of,
        duplicate_count=duplicate_count,
        beyond_window=beyond_window,
        last_notified_at=last_notified_at
    )


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
    reanalyzed: bool
) -> SaveWebhookResult | None:
    original = await session.get(WebhookEvent, original_event.id)
    if not original:
        return None

    original.duplicate_count = (original.duplicate_count or 1) + 1
    original.updated_at = datetime.now()
    logger.info(f"发现重复告警，原始告警ID={original.id}, 已重复{original.duplicate_count}次")

    final_ai_analysis, final_importance = _resolve_analysis_for_duplicate(ai_analysis, original, reanalyzed)
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
        beyond_window=1 if beyond_window else 0
    )

    session.add(duplicate_event)
    await session.flush()

    if ai_analysis:
        logger.info(f"重复告警已保存: ID={duplicate_event.id}, 使用传入的AI分析结果")
    elif original.ai_analysis:
        logger.info(
            f"重复告警已保存: ID={duplicate_event.id}, "
            f"复用原始告警 {original.id} 的AI分析结果"
        )
    else:
        logger.info(f"重复告警已保存: ID={duplicate_event.id}, 无AI分析结果")

    if Config.ENABLE_FILE_BACKUP:
        save_webhook_to_file(data, source, raw_payload, headers, client_ip, final_ai_analysis)

    return SaveWebhookResult(duplicate_event.id, True, original.id, beyond_window)


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
    forward_status: str
) -> SaveWebhookResult:
    webhook_event = _build_event(
        source=source,
        client_ip=client_ip,
        raw_payload=raw_payload,
        headers=headers,
        data=data,
        alert_hash=alert_hash,
        ai_analysis=ai_analysis,
        importance=ai_analysis.get('importance') if ai_analysis else None,
        forward_status=forward_status,
        is_duplicate=0,
        duplicate_of=None,
        duplicate_count=1,
        beyond_window=0,
        last_notified_at=datetime.now()
    )

    session.add(webhook_event)
    await session.flush()
    logger.info(f"Webhook 数据已保存到数据库: ID={webhook_event.id}")

    if Config.ENABLE_FILE_BACKUP:
        save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)

    return SaveWebhookResult(webhook_event.id, False, None, False)


def _save_to_file_fallback(
    data: WebhookData,
    source: str,
    raw_payload: bytes | None,
    headers: HeadersDict | None,
    client_ip: str | None,
    ai_analysis: AnalysisResult | None
) -> SaveWebhookResult:
    file_id = save_webhook_to_file(data, source, raw_payload, headers, client_ip, ai_analysis)
    return SaveWebhookResult(file_id, False, None, False)


async def save_webhook_data(
    data: WebhookData,
    source: str = 'unknown',
    raw_payload: bytes | None = None,
    headers: HeadersDict | None = None,
    client_ip: str | None = None,
    ai_analysis: AnalysisResult | None = None,
    forward_status: str = 'pending',
    alert_hash: str | None = None,
    is_duplicate: bool | None = None,
    original_event: WebhookEvent | None = None,
    beyond_window: bool = False,
    reanalyzed: bool = False
) -> SaveWebhookResult:
    """保存 webhook 数据到数据库（带重试机制防止并发竞态）。"""
    if alert_hash is None:
        alert_hash = generate_alert_hash(data, source)

    for attempt in range(MAX_SAVE_RETRIES):
        try:
            async with session_scope() as session:
                # 在同一事务内重新判重，避免外层结果在高并发下过期。
                if is_duplicate is None:
                    duplicate_check = await check_duplicate_alert(
                        alert_hash,
                        session=session
                    )
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
                        reanalyzed=reanalyzed
                    )
                    if saved:
                        return saved

                return await _save_new_event(
                    session,
                    source=source,
                    client_ip=client_ip,
                    raw_payload=raw_payload,
                    headers=headers,
                    data=data,
                    alert_hash=alert_hash,
                    ai_analysis=ai_analysis,
                    forward_status=forward_status
                )

        except IntegrityError as e: # noqa: PERF203
            logger.warning(f"检测到并发插入冲突 (attempt {attempt + 1}/{MAX_SAVE_RETRIES}): {e!s}")

            if attempt < MAX_SAVE_RETRIES - 1:
                # 指数退避让并发写入先完成，再次判重时更容易命中已落库记录。
                time.sleep(RETRY_DELAY_SECONDS * (2 ** attempt))
                is_duplicate = None
                original_event = None
                logger.info(f"正在重试... (attempt {attempt + 2}/{MAX_SAVE_RETRIES})")
                continue

            # 最后兜底：直接读最新原始告警并降级写入重复记录，避免请求彻底失败。
            logger.error(f"重试 {MAX_SAVE_RETRIES} 次后仍然失败，尝试最后查找")
            async with session_scope() as fallback_session:
                existing = await _query_latest_original_event(fallback_session, alert_hash)

                if not existing:
                    logger.error(f"并发冲突但无法找到原始告警: hash={alert_hash}")
                    raise

                logger.info(f"最终找到原始告警 ID={existing.id}，标记为重复")
                existing.duplicate_count += 1

                final_ai_analysis = ai_analysis if ai_analysis else existing.ai_analysis
                final_importance = ai_analysis.get('importance') if ai_analysis else existing.importance

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
                    duplicate_of=existing.id,
                    duplicate_count=existing.duplicate_count,
                    beyond_window=1 if beyond_window else 0
                )
                fallback_session.add(dup_event)
                await fallback_session.flush()
                return SaveWebhookResult(dup_event.id, True, existing.id, beyond_window)

        except Exception as e:
            logger.error(f"保存 webhook 数据到数据库失败: {e!s}")
            return _save_to_file_fallback(data, source, raw_payload, headers, client_ip, ai_analysis)

    logger.error("保存数据异常：退出重试循环但未返回结果")
    return _save_to_file_fallback(data, source, raw_payload, headers, client_ip, ai_analysis)


def save_webhook_to_file(
    data: WebhookData,
    source: str = 'unknown',
    raw_payload: bytes | None = None,
    headers: HeadersDict | None = None,
    client_ip: str | None = None,
    ai_analysis: AnalysisResult | None = None
) -> str:
    """保存 webhook 数据到文件(备份方式)"""
    # 创建数据目录
    os.makedirs(Config.DATA_DIR, exist_ok=True)

    # 生成文件名(基于时间戳)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    filename = f"{source}_{timestamp}.json"
    filepath = str(Path(Config.DATA_DIR) / filename)

    # 准备保存的完整数据
    full_data = {
        'timestamp': datetime.now().isoformat(),
        'source': source,
        'client_ip': client_ip,
        'headers': dict(headers) if headers else {},
        'raw_payload': raw_payload.decode('utf-8') if raw_payload else None,
        'parsed_data': data
    }

    # 添加 AI 分析结果
    if ai_analysis:
        full_data['ai_analysis'] = ai_analysis

    # 保存数据
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(full_data, f, indent=2, ensure_ascii=False)

    return filepath


def get_client_ip(request: Request) -> str:
    """获取客户端 IP 地址"""
    forwarded_for = request.headers.get('x-forwarded-for')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()

    real_ip = request.headers.get('x-real-ip')
    if real_ip:
        return real_ip

    return request.client.host if request.client else 'unknown'


async def get_all_webhooks(
    page: int = 1,
    page_size: int = 20,
    cursor_id: int | None = None,
    fields: str = 'summary'
) -> tuple[list[dict], int, int | None]:
    """
    从数据库获取 webhook 数据（支持游标分页和字段选择）

    Args:
        page: 页码（仅用于首次加载或无游标时）
        page_size: 每页数量
        cursor_id: 游标 ID，获取此 ID 之后的数据（更高效）
        fields: 字段选择 - 'summary'(摘要), 'full'(完整)

    Returns:
        tuple: (webhook数据列表, 总数量, 下一页游标ID)
    """
    try:
        async with session_scope() as session:
            # 查询总数
            total_stmt = select(func.count()).select_from(WebhookEvent)
            total_result = await session.execute(total_stmt)
            total = total_result.scalar()

            # 构建查询
            query = select(WebhookEvent)

            # 筛选条件
            if cursor_id is not None:
                # 游标分页：获取 ID 小于 cursor_id 的记录（因为按 ID 降序）
                query = query.filter(WebhookEvent.id < cursor_id)

            # 先排序（必须在 offset 和 limit 之前）
            query = query.order_by(WebhookEvent.id.desc())

            # 再分页
            if cursor_id is None:
                # 无游标时使用 offset（仅首次加载）
                offset = (page - 1) * page_size
                if offset > 0:
                    query = query.offset(offset)

            # 最后限制数量
            query = query.limit(page_size)
            result = await session.execute(query)
            events = result.scalars().all()

            # 根据 fields 参数决定返回哪些字段
            if fields == 'summary':
                # 摘要模式：只返回列表必需的字段，减少数据传输量
                webhooks = [event.to_summary_dict() for event in events]
            else:
                # 完整模式：返回所有字段
                webhooks = [event.to_dict() for event in events]

            # 为重复告警添加窗口信息和上次告警 ID（批量计算优化）
            # 直接从数据库字段读取，无需动态计算
            for webhook in webhooks:
                # beyond_window 已经在数据库中固化，直接使用
                beyond_window = bool(webhook.get('beyond_window', 0))
                webhook['beyond_time_window'] = beyond_window
                webhook['is_within_window'] = not beyond_window if webhook.get('is_duplicate') else False

            # 批量计算上次告警 ID（优化性能）
            # 收集所有需要查询的 (hash, timestamp)
            lookup_map = {}
            for webhook in webhooks:
                if webhook.get('alert_hash'):
                    try:
                        current_timestamp = datetime.fromisoformat(webhook['timestamp'])
                        key = (webhook['alert_hash'], current_timestamp)
                        lookup_map[key] = webhook
                    except Exception as e:
                        logger.warning(f"解析时间戳失败 (webhook={webhook.get('id')}): {e}")
                        webhook['prev_alert_id'] = None

            # 批量查询所有的上一条记录（一次查询）
            if lookup_map:
                try:
                    # 获取所有涉及的 alert_hash
                    all_hashes = list({k[0] for k in lookup_map})

                    # 查询这些 hash 的所有记录（去重需要）
                    all_alerts_stmt = select(WebhookEvent.id, WebhookEvent.alert_hash, WebhookEvent.timestamp)\
                        .filter(WebhookEvent.alert_hash.in_(all_hashes))\
                        .order_by(WebhookEvent.alert_hash, WebhookEvent.timestamp.desc())
                    result = await session.execute(all_alerts_stmt)
                    all_alerts = result.all()

                    # 构建 hash -> 按时间排序的记录列表
                    hash_to_alerts = {}
                    for alert_id, alert_hash, alert_timestamp in all_alerts:
                        if alert_hash not in hash_to_alerts:
                            hash_to_alerts[alert_hash] = []
                        hash_to_alerts[alert_hash].append((alert_id, alert_timestamp))

                    # 为每个 webhook 找到上一条记录
                    for (alert_hash, current_timestamp), webhook in lookup_map.items():
                        alerts_list = hash_to_alerts.get(alert_hash, [])
                        # 找到时间早于当前的第一条
                        prev_id = None
                        prev_timestamp = None
                        for aid, ats in alerts_list:
                            if ats < current_timestamp:
                                prev_id = aid
                                prev_timestamp = ats
                                break
                        webhook['prev_alert_id'] = prev_id
                        webhook['prev_alert_timestamp'] = prev_timestamp.isoformat() if prev_timestamp else None
                except Exception as e:
                    logger.warning(f"批量计算 prev_alert_id 失败: {e}")
                    # 失败时设置为 None
                    for webhook in lookup_map.values():
                        webhook['prev_alert_id'] = None
                        webhook['prev_alert_timestamp'] = None

            # 没有 alert_hash 的设为 None
            for webhook in webhooks:
                if not webhook.get('alert_hash'):
                    webhook['prev_alert_id'] = None
                    webhook['prev_alert_timestamp'] = None

            # 计算下一页游标
            next_cursor = events[-1].id if events else None

            return webhooks, total, next_cursor

    except Exception as e:
        logger.error(f"从数据库查询 webhook 数据失败: {e!s}")
        webhooks = get_webhooks_from_files(limit=page_size)
        return webhooks, len(webhooks), None


def get_webhooks_from_files(limit: int = 50) -> list[dict]:
    """从文件获取 webhook 数据(备份方式)"""
    if not os.path.exists(Config.DATA_DIR):
        return []

    webhooks = []
    files = [f for f in os.listdir(Config.DATA_DIR) if f.endswith('.json')]

    # 读取所有文件
    for filename in files:
        filepath = str(Path(Config.DATA_DIR) / filename)
        try:
            with open(filepath, encoding='utf-8') as f:
                webhook_data = json.load(f)
                webhook_data['filename'] = filename
                webhooks.append(webhook_data)
        except Exception as e:
            logger.error(f"读取文件失败 {filename}: {e!s}")

    # 按 timestamp 字段倒序排序（最新的在前面）
    webhooks.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

    # 返回限制数量的结果
    return webhooks[:limit]




MAX_SAVE_RETRIES = Config.SAVE_MAX_RETRIES
RETRY_DELAY_SECONDS = Config.SAVE_RETRY_DELAY_SECONDS
