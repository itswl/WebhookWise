"""Webhook 处理主管线 — 纯协调层，业务逻辑委托给子模块。"""

import asyncio

import httpx
import orjson
import sqlalchemy.exc
from sqlalchemy import update

try:
    from asyncpg.exceptions import QueryCanceledError as _QueryCanceledError
except ImportError:
    _QueryCanceledError = None  # 兼容无 asyncpg 环境

from api import InvalidJsonError
from core.compression import decompress_payload_async
from core.logger import logger
from core.metrics import (
    WEBHOOK_DEAD_LETTER_TOTAL,
    WEBHOOK_NOISE_REDUCED_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    WEBHOOK_RUNNING_TASKS,
    sanitize_source,
)
from core.trace import generate_trace_id, set_trace_id
from core.utils import generate_alert_hash, processing_lock
from db.session import session_scope
from models import WebhookEvent
from services.pipeline_analysis import resolve_analysis
from services.pipeline_forward import _refresh_original_event, decide_forwarding, execute_forwarding
from services.pipeline_noise import _apply_noise_metadata, persist_webhook_with_noise_context
from services.pipeline_request import _build_webhook_response, _parse_webhook_request

_NON_RETRYABLE_ERRORS = (
    ValueError,
    KeyError,
    TypeError,
    orjson.JSONDecodeError,
    UnicodeDecodeError,
)


async def _send_dead_letter_alert(event_id: int, retry_count: int, error: Exception) -> None:
    """发送 Dead Letter 飞书告警。复用项目已有的飞书通知通道。"""
    try:
        from core.config import Config
        from core.http_client import get_http_client

        forward_url = getattr(Config.ai, "FORWARD_URL", "") or ""
        if not forward_url or not ("feishu.cn" in forward_url or "lark" in forward_url):
            logger.critical(
                "[Pipeline] Dead Letter 告警: event_id=%s, retry_count=%d, error=%s",
                event_id,
                retry_count,
                error,
            )
            return

        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "\U0001f6a8 Dead Letter \u544a\u8b66"},
                    "template": "red",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**event_id**: {event_id}\n"
                                f"**\u91cd\u8bd5\u6b21\u6570**: {retry_count}\n"
                                f"**\u9519\u8bef\u7c7b\u578b**: {type(error).__name__}\n"
                                f"**\u9519\u8bef\u8be6\u60c5**: {str(error)[:200]}"
                            ),
                        },
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "\u8be5\u4e8b\u4ef6\u5df2\u8017\u5c3d\u6240\u6709\u91cd\u8bd5\u6b21\u6570\uff0c\u8bf7\u4eba\u5de5\u6392\u67e5\u5904\u7406\u3002",
                            }
                        ],
                    },
                ],
            },
        }

        client = get_http_client()
        resp = await client.post(forward_url, json=card, timeout=10)
        if resp.status_code != 200:
            logger.warning(
                "[Pipeline] Dead Letter \u98de\u4e66\u544a\u8b66\u53d1\u9001\u5931\u8d25: status=%s", resp.status_code
            )
    except Exception:
        logger.warning("[Pipeline] Dead Letter \u544a\u8b66\u53d1\u9001\u5931\u8d25", exc_info=True)


# 最大重试次数，超过后标记为 dead_letter 不再捕捉
_MAX_RETRIES = 5

# 正在运行的 webhook 处理任务跟踪（供优雅停机使用）
_running_tasks: set[asyncio.Task] = set()


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否可重试。"""
    # 网络相关异常：可重试
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, ConnectionError, OSError)):
        return True
    # DB statement_timeout 触发时 asyncpg 抛出 QueryCanceledError：可重试
    if _QueryCanceledError and isinstance(exc, _QueryCanceledError):
        return True
    # SQLAlchemy 层面的操作错误（含超时透传）：可重试
    if isinstance(exc, sqlalchemy.exc.OperationalError):
        return True
    # 明确的数据格式/校验错误：不可重试，其他未知异常：默认可重试（保守策略）
    return not isinstance(exc, _NON_RETRYABLE_ERRORS)


def get_running_tasks() -> set[asyncio.Task]:
    """获取当前正在运行的 webhook 处理任务集合。"""
    return _running_tasks


async def _update_processing_status(event_id: int | None, status: str) -> None:
    """更新 webhook 事件的处理状态（Direct UPDATE，无需先 SELECT）"""
    if event_id is None:
        return
    try:
        async with session_scope() as session:
            await session.execute(
                update(WebhookEvent).where(WebhookEvent.id == event_id).values(processing_status=status)
            )
    except Exception as e:
        logger.warning(f"[Pipeline] 更新 processing_status 失败: event_id={event_id}, status={status}, error={e!s}")


async def _increment_and_get_retry_count(event_id: int | None) -> int:
    """原子递增 retry_count 并返回递增后的值。"""
    if event_id is None:
        return 0
    try:
        async with session_scope() as session:
            result = await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id == event_id)
                .values(retry_count=WebhookEvent.retry_count + 1)
                .returning(WebhookEvent.retry_count)
            )
            row = result.scalar_one_or_none()
            return row if row is not None else 0
    except Exception as e:
        logger.warning(f"[Pipeline] 递增 retry_count 失败: event_id={event_id}, error={e!s}")
        return 0


async def handle_webhook_process(
    event_id: int,
    client_ip: str = "",
):
    """处理 webhook 的主流程入口。

    仅接收 event_id，从 DB 加载完整事件数据，避免持有大对象。
    并发控制由 Redis Stream 的 count 参数自然限流，无需进程内 Semaphore。
    """
    set_trace_id(generate_trace_id(event_id=event_id))
    task = asyncio.current_task()
    if task:
        _running_tasks.add(task)
        WEBHOOK_RUNNING_TASKS.inc()
    try:
        await _handle_webhook_process_inner(event_id, client_ip)
    finally:
        if task:
            _running_tasks.discard(task)
            WEBHOOK_RUNNING_TASKS.dec()


async def _handle_webhook_process_inner(
    event_id: int,
    client_ip: str = "",
):
    # ── 合并会话：加载事件 + 更新状态为 analyzing（单次 checkout/checkin）──
    async with session_scope() as session:
        stmt = (
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(processing_status="analyzing")
            .returning(WebhookEvent)
        )
        result = await session.execute(stmt)
        event = result.scalar_one_or_none()
        if not event:
            logger.error(f"[Pipeline] Event {event_id} not found in DB")
            return

        # 在 session 关闭前提取所有需要的字段到局部变量
        headers = event.headers or {}
        raw_payload = await decompress_payload_async(event.raw_payload) or ""
        raw_body = raw_payload.encode("utf-8") if isinstance(raw_payload, str) else b""
        source = event.source

        # 延迟解析：网关零解析模式下 parsed_data 为 None，从 raw_payload 恢复
        if event.parsed_data is None and raw_payload:
            try:
                payload = orjson.loads(raw_payload)
            except Exception:
                payload = {}
        else:
            payload = event.parsed_data or {}

    logger.debug(f"[Pipeline] 开始处理流程: source={source or 'unknown'}, event_id={event_id}")
    WEBHOOK_RECEIVED_TOTAL.labels(source=sanitize_source(source), status="received").inc()
    analysis_result = {}
    original_event = None

    try:
        try:
            request_context = _parse_webhook_request(client_ip, headers, payload, raw_body, source)
        except InvalidJsonError:
            return

        alert_hash = generate_alert_hash(request_context.parsed_data, request_context.source)

        async with processing_lock(alert_hash) as got_lock:
            logger.debug("[Pipeline] 进入 AI 分析阶段")
            analysis_resolution = await resolve_analysis(alert_hash, request_context.webhook_full_data, got_lock)

            analysis_result = analysis_resolution.analysis_result
            original_event = analysis_resolution.original_event

            if analysis_resolution.is_reused:
                # ── 轻量路径：缓存复用的 Worker 跳过降噪重算和转发 ──
                logger.info("[Pipeline] 缓存命中轻量路径：跳过降噪计算与转发（由持锁 Worker 负责）")
                persisted = await persist_webhook_with_noise_context(
                    request_context=request_context,
                    analysis_resolution=analysis_resolution,
                    alert_hash=alert_hash,
                    event_id=event_id,
                    skip_noise=True,
                )

                save_result = persisted.save_result
                noise_context = persisted.noise_context
                analysis_result = _apply_noise_metadata(analysis_result, noise_context)

                WEBHOOK_NOISE_REDUCED_TOTAL.labels(
                    source=sanitize_source(request_context.source),
                    relation=noise_context.relation,
                    suppressed=str(noise_context.suppress_forward).lower(),
                ).inc()

                logger.info(f"[Pipeline] 轻量路径处理完成: id={save_result.webhook_id}, reused=True")

                if event_id is None:
                    await _update_processing_status(event_id, "completed")

                return _build_webhook_response(
                    save_result.webhook_id,
                    analysis_result,
                    {"status": "skipped", "reason": "缓存复用路径，由持锁 Worker 负责转发"},
                    save_result.is_duplicate,
                    save_result.original_id,
                    save_result.beyond_window,
                    save_result.is_duplicate and not save_result.beyond_window,
                )

            # ── 完整路径：降噪 + 持久化 + 转发 ──
            logger.debug("[Pipeline] 进入持久化与降噪计算阶段")
            persisted = await persist_webhook_with_noise_context(
                request_context=request_context,
                analysis_resolution=analysis_resolution,
                alert_hash=alert_hash,
                event_id=event_id,
            )

            save_result = persisted.save_result
            noise_context = persisted.noise_context
            analysis_result = _apply_noise_metadata(analysis_result, noise_context)

            WEBHOOK_NOISE_REDUCED_TOTAL.labels(
                source=sanitize_source(request_context.source),
                relation=noise_context.relation,
                suppressed=str(noise_context.suppress_forward).lower(),
            ).inc()

        # ── 锁外：转发阶段（无需持有 processing_lock）──
        beyond_window = save_result.beyond_window
        is_dup = save_result.is_duplicate
        original_id = save_result.original_id
        is_duplicate = is_dup and not beyond_window
        importance = str(analysis_result.get("importance", "")).lower()

        original_event = await _refresh_original_event(original_id, original_event)

        logger.debug("[Pipeline] 进入转发决策阶段")
        forward_decision = await decide_forwarding(
            importance,
            is_duplicate,
            beyond_window,
            noise_context,
            original_event,
            original_id,
            source=request_context.source,
        )

        forward_result = await execute_forwarding(
            forward_decision, request_context, analysis_result, persisted, original_event
        )

        logger.info(
            f"[Pipeline] 处理流程结束: id={save_result.webhook_id}, forwarded={forward_decision.should_forward}"
        )

        # processing_status 已在 save_webhook_data 事务中统一设为 "completed"，
        # 仅当无 event_id（向后兼容旧路径）时才单独更新。
        if event_id is None:
            await _update_processing_status(event_id, "completed")

        return _build_webhook_response(
            save_result.webhook_id, analysis_result, forward_result, is_dup, original_id, beyond_window, is_duplicate
        )

    except Exception as e:
        if _is_retryable(e):
            # 先递增 retry_count 并判断是否超过最大重试次数
            current_retry = await _increment_and_get_retry_count(event_id)
            if current_retry >= _MAX_RETRIES:
                logger.warning(
                    "Webhook 处理失败（重试次数已耗尽，标记为死信）| event_id=%s | retry_count=%d | error=%s",
                    event_id,
                    current_retry,
                    e,
                )
                await _update_processing_status(event_id, "dead_letter")
                WEBHOOK_DEAD_LETTER_TOTAL.inc()
                await _send_dead_letter_alert(event_id, current_retry, e)
            else:
                logger.error(
                    "Webhook 处理失败（可重试，退回 received 等待 RecoveryPoller）| event_id=%s | retry=%d/%d | error=%s",
                    event_id,
                    current_retry,
                    _MAX_RETRIES,
                    e,
                    exc_info=True,
                )
                await _update_processing_status(event_id, "received")
        else:
            logger.warning(
                "Webhook 处理失败（不可重试，标记为死信）| event_id=%s | error=%s | type=%s",
                event_id,
                e,
                type(e).__name__,
            )
            await _update_processing_status(event_id, "dead_letter")
            WEBHOOK_DEAD_LETTER_TOTAL.inc()
            await _send_dead_letter_alert(event_id, 0, e)
        return
