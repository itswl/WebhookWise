"""Webhook 处理主管线 — 纯协调层，业务逻辑委托给子模块。"""

from sqlalchemy import select, update

from api import InvalidJsonError, InvalidSignatureError
from core.logger import logger
from core.metrics import WEBHOOK_NOISE_REDUCED_TOTAL, WEBHOOK_RECEIVED_TOTAL
from core.utils import generate_alert_hash, processing_lock
from db.session import session_scope
from models import WebhookEvent
from services.pipeline_analysis import resolve_analysis
from services.pipeline_forward import _refresh_original_event, decide_forwarding, execute_forwarding
from services.pipeline_noise import _apply_noise_metadata, persist_webhook_with_noise_context
from services.pipeline_request import _build_webhook_response, _parse_webhook_request


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


async def handle_webhook_process(
    event_id: int,
    client_ip: str = "",
):
    """处理 webhook 的主流程入口。

    仅接收 event_id，从 DB 加载完整事件数据，避免 BackgroundTasks 持有大对象。
    """
    # 从 DB 加载事件记录
    async with session_scope() as session:
        result = await session.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))
        event = result.scalar_one_or_none()
        if not event:
            logger.error(f"[Pipeline] Event {event_id} not found in DB")
            return

        headers = event.headers or {}
        payload = event.parsed_data or {}
        raw_payload = event.raw_payload or ""
        raw_body = raw_payload.encode("utf-8") if isinstance(raw_payload, str) else b""
        source = event.source

    logger.info(f"[Pipeline] 开始处理流程: source={source or 'unknown'}, event_id={event_id}")
    WEBHOOK_RECEIVED_TOTAL.labels(source=source or "unknown", status="received").inc()
    analysis_result = {}
    original_event = None

    try:
        await _update_processing_status(event_id, "analyzing")

        try:
            request_context = _parse_webhook_request(client_ip, headers, payload, raw_body, source)
        except InvalidSignatureError:
            logger.warning(f"认证失败 (Token/Signature 不匹配或缺失): IP={client_ip}, Source={source or 'unknown'}")
            return
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
                    source=request_context.source,
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
                source=request_context.source,
                relation=noise_context.relation,
                suppressed=str(noise_context.suppress_forward).lower(),
            ).inc()

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
        logger.error(f"处理 Webhook 时发生错误: {e!s}", exc_info=True)
        await _update_processing_status(event_id, "failed")
        return
