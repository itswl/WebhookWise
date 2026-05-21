"""OpenClaw analysis result polling.

Each pending DeepAnalysis record schedules its own TaskIQ one-shot poll. The DB
stores audit state, while TaskIQ owns retry/poll timing.
"""

import asyncio
import contextlib
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

from core.http_client import get_http_client
from core.observability.metrics import DEEP_ANALYSIS_TOTAL
from core.observability.tracing import get_current_trace_id
from services.analysis.openclaw_http import poll_openclaw_final
from services.analysis.openclaw_poll_policy import OpenClawPollPolicy
from services.analysis.openclaw_result_parser import (
    build_analysis_result_from_openclaw_text,
    extract_robust_json,
)
from services.operations.deep_analysis_notifications import (
    send_deep_analysis_failure_notification,
    send_deep_analysis_success_notification,
)
from services.webhooks.types import DeepAnalysisStatus, WebhookData

logger = logging.getLogger("webhook_service.openclaw_poller")
MANUAL_RETRY_STARTED_AT_KEY = "_manual_retry_started_at"


async def _safe_notify(coro: Any) -> None:
    try:
        await coro
    except Exception as e:
        logger.warning("[Poller] 后台通知失败: %s", e)


def _seconds_until(target: datetime) -> int:
    return max(1, int((target - datetime.now()).total_seconds()))


def _clamp_poll_delay_to_timeout(
    delay_seconds: int, created_at: datetime | None, *, policy: OpenClawPollPolicy | None = None
) -> int:
    return (policy or OpenClawPollPolicy.from_config()).clamp_delay_to_timeout(delay_seconds, created_at)


def _poll_claim_lease_seconds(policy: OpenClawPollPolicy | None = None) -> int:
    """How long a claimed poll stays hidden from scanner fallback."""
    return (policy or OpenClawPollPolicy.from_config()).poll_claim_lease_seconds


def _openclaw_http_poll_timeout(policy: OpenClawPollPolicy | None = None) -> float:
    return (policy or OpenClawPollPolicy.from_config()).http_poll_timeout


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _poll_timeout_started_at(rec: WebhookData) -> datetime | None:
    analysis_result = rec.get("analysis_result")
    if isinstance(analysis_result, dict):
        manual_retry_started_at = analysis_result.get(MANUAL_RETRY_STARTED_AT_KEY)
        if isinstance(manual_retry_started_at, str) and manual_retry_started_at:
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(manual_retry_started_at)

    created_at = rec.get("created_at")
    return created_at if isinstance(created_at, datetime) else None


def _is_transient_poll_error(error: object) -> bool:
    if not error:
        return False
    text = str(error).lower()
    transient_markers = (
        "all connection attempts failed",
        "connection refused",
        "connection reset",
        "connect call failed",
        "network is unreachable",
        "no route to host",
        "name or service not known",
        "temporary failure",
        "timed out",
        "timeout",
    )
    return any(marker in text for marker in transient_markers)


async def _get_poll_stability(record_id: int) -> WebhookData | None:
    from core.redis_client import redis_get_json_dict
    from core.redis_keys import openclaw_poller_stability

    return await redis_get_json_dict(openclaw_poller_stability(record_id))


async def _set_poll_stability(record_id: int, data: WebhookData) -> None:
    from core.redis_client import redis_setex_json
    from core.redis_keys import openclaw_poller_stability

    await redis_setex_json(openclaw_poller_stability(record_id), 3600, data)


async def _clear_poll_stability(record_id: int) -> None:
    from core.redis_client import redis_delete
    from core.redis_keys import openclaw_poller_stability

    await redis_delete(openclaw_poller_stability(record_id))


async def clear_openclaw_poll_state(record_id: int) -> None:
    """Clear transient poller cache before a manual retry."""
    await _clear_poll_stability(record_id)


async def _notify_feishu_deep_analysis(
    record_dict: WebhookData, source: str = "", *, policy: OpenClawPollPolicy | None = None
) -> None:
    """Compatibility wrapper for the old poller notification hook."""
    await send_deep_analysis_success_notification(record_dict, source, policy=policy)


async def notify_deep_analysis_success(
    record: Any, source: str = "", *, policy: OpenClawPollPolicy | None = None
) -> None:
    record_dict = {
        "id": record.id,
        "webhook_event_id": record.webhook_event_id,
        "engine": record.engine,
        "analysis_result": record.analysis_result,
        "duration_seconds": record.duration_seconds,
    }
    await _notify_feishu_deep_analysis(record_dict, source, policy=policy)


async def _notify_feishu_deep_analysis_failed(
    record_dict: WebhookData, reason: str = "", *, policy: OpenClawPollPolicy | None = None
) -> None:
    """Compatibility wrapper for the old poller failure-notification hook."""
    await send_deep_analysis_failure_notification(record_dict, reason, policy=policy)


async def _poll_via_http(
    session_key: str,
    retry_count: int = 3,
    *,
    policy: OpenClawPollPolicy | None = None,
    http_client: Any | None = None,
) -> WebhookData:
    """Compatibility wrapper for HTTP /final polling."""
    policy = policy or OpenClawPollPolicy.from_config()
    return await poll_openclaw_final(
        session_key,
        policy=policy,
        http_client=http_client or get_http_client(),
        trace_id=get_current_trace_id(),
        retry_count=retry_count,
    )


async def _poll_single_record(rec: WebhookData, *, policy: OpenClawPollPolicy | None = None) -> WebhookData:
    """对单条 pending 记录执行 HTTP 轮询 + 稳定性检查（完全脱离 DB）。

    返回一个 dict 描述本次轮询的处理结果，供阶段 3 写回 DB。
    返回格式::

        {"id": int, "action": "skip" | "update", ...更新字段}
    """
    from services.analysis.openclaw_ws_client import poll_session_result
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    policy = policy or OpenClawPollPolicy.from_config()
    record_id = rec["id"]

    try:
        timeout_started_at = _poll_timeout_started_at(rec)
        # --- 超时检查 ---
        timeout_seconds = policy.timeout_seconds
        elapsed_total = (datetime.now() - timeout_started_at).total_seconds() if timeout_started_at else 0.0
        if timeout_started_at and elapsed_total > timeout_seconds:
            logger.info("[Poller] 分析超时: id=%s elapsed=%.0fs timeout=%ss", record_id, elapsed_total, timeout_seconds)
            await _clear_poll_stability(record_id)
            DEEP_ANALYSIS_TOTAL.labels(status="timeout", engine=rec.get("engine", "openclaw")).inc()
            update: WebhookData = {
                "status": DeepAnalysisStatus.FAILED,
                "analysis_result": {"root_cause": "OpenClaw 分析超时"},
            }
            notify_dict = {**rec, **update}
            await _notify_feishu_deep_analysis_failed(notify_dict, "超时失败", policy=policy)
            return {"id": record_id, "action": "update", **update}

        # --- session_key 缺失检查 ---
        if not rec["openclaw_session_key"]:
            elapsed = (datetime.now() - timeout_started_at).total_seconds() if timeout_started_at else 999.0
            if elapsed < compute_openclaw_poll_delay(0, policy=policy):
                return {"id": record_id, "action": "skip"}
            logger.warning("[Poller] 缺少 session_key，标记失败: id=%s elapsed=%.0fs", record_id, elapsed)
            DEEP_ANALYSIS_TOTAL.labels(status="failed", engine=rec.get("engine", "openclaw")).inc()
            update = {
                "status": DeepAnalysisStatus.FAILED,
                "analysis_result": {
                    "root_cause": "无法获取分析会话，OpenClaw 触发失败",
                    "error": "missing_session_key",
                    "failure_reason": "未能获取到分析会话密钥",
                },
            }
            await _clear_poll_stability(record_id)
            notify_dict = {**rec, **update}
            await _notify_feishu_deep_analysis_failed(notify_dict, "无 session_key - OpenClaw 触发失败", policy=policy)
            return {"id": record_id, "action": "update", **update}

        # --- HTTP 轮询 ---
        if policy.has_http_api:
            result = await _poll_via_http(rec["openclaw_session_key"], policy=policy)
        else:
            result = await poll_session_result(
                gateway_url=policy.gateway_url,
                gateway_token=policy.gateway_token,
                session_key=rec["openclaw_session_key"],
                timeout=policy.poll_timeout_seconds,
            )

        # --- 处理 completed ---
        if result.get("status") == "completed":
            text = result.get("text", "")
            msg_count = int(result.get("msg_count", 0) or 0)
            required_hits = 1 if result.get("is_final") is True else policy.stability_required_hits

            def _completed_update() -> WebhookData:
                analysis_result = build_analysis_result_from_openclaw_text(text, str(rec["openclaw_run_id"] or ""))
                duration = (datetime.now() - timeout_started_at).total_seconds() if timeout_started_at else 0.0
                DEEP_ANALYSIS_TOTAL.labels(status="completed", engine=rec.get("engine", "openclaw")).inc()
                return {
                    "id": record_id,
                    "action": "update",
                    "_need_success_notify": True,
                    "status": DeepAnalysisStatus.COMPLETED,
                    "analysis_result": analysis_result,
                    "duration_seconds": duration,
                }

            if required_hits <= 1:
                logger.info("[Poller] 分析完成，稳定命中阈值为 1，直接写库: id=%s", record_id)
                await _clear_poll_stability(record_id)
                return _completed_update()

            current_snapshot = {"msg_count": msg_count, "text_len": len(text), "text_hash": _text_hash(text)}
            prev_snapshot = await _get_poll_stability(record_id)

            if (
                prev_snapshot
                and prev_snapshot.get("msg_count") == current_snapshot["msg_count"]
                and prev_snapshot.get("text_len") == current_snapshot["text_len"]
                and prev_snapshot.get("text_hash") == current_snapshot["text_hash"]
            ):
                hit_count = prev_snapshot.get("hit_count", 1) + 1
                logger.info(
                    "[Poller] 结果稳定检查: id=%s hit=%s/%s msg_count=%s text_len=%s",
                    record_id,
                    hit_count,
                    required_hits,
                    msg_count,
                    len(text),
                )
                if hit_count >= required_hits:
                    logger.info("[Poller] 分析稳定确认，准备写库: id=%s", record_id)
                else:
                    await _set_poll_stability(record_id, {**current_snapshot, "hit_count": hit_count})
                    return {"id": record_id, "action": "skip"}

                await _clear_poll_stability(record_id)
                return _completed_update()
            else:
                logger.info(
                    "[Poller] 首次或结果变化，等待稳定: id=%s msg_count=%s text_len=%s",
                    record_id,
                    msg_count,
                    len(text),
                )
                await _set_poll_stability(
                    record_id, {**current_snapshot, "hit_count": 1, "first_result": {"text": text}}
                )
                return {"id": record_id, "action": "skip"}

        # --- 处理 error ---
        elif result.get("status") == "error":
            prev_snapshot = await _get_poll_stability(record_id)
            if prev_snapshot and "first_result" in prev_snapshot:
                error_count = prev_snapshot.get("error_count", 0) + 1
                if error_count >= policy.max_consecutive_errors and policy.enable_degradation:
                    text = prev_snapshot["first_result"]["text"]
                    logger.warning(
                        "[Poller] 连续错误达阈值，降级使用首次结果: id=%s error_count=%d", record_id, error_count
                    )
                    await _clear_poll_stability(record_id)
                    DEEP_ANALYSIS_TOTAL.labels(status="degraded", engine=rec.get("engine", "openclaw")).inc()
                    return {
                        "id": record_id,
                        "action": "update",
                        "status": DeepAnalysisStatus.COMPLETED,
                        "analysis_result": build_analysis_result_from_openclaw_text(
                            text, str(rec["openclaw_run_id"] or "")
                        ),
                    }
                # 更新 error_count 并继续等待
                await _set_poll_stability(record_id, {**prev_snapshot, "error_count": error_count})
                return {"id": record_id, "action": "skip"}

            error_msg = result.get("error", "OpenClaw 返回错误")
            if bool(result.get("retryable")) or _is_transient_poll_error(error_msg):
                logger.warning(
                    "[Poller] OpenClaw 轮询遇到临时错误，保留 pending 等待下轮重试: id=%s error=%s",
                    record_id,
                    error_msg,
                )
                return {"id": record_id, "action": "skip"}

            await _clear_poll_stability(record_id)
            update = {
                "status": DeepAnalysisStatus.FAILED,
                "analysis_result": {
                    "root_cause": error_msg,
                    "error": error_msg,
                    "failure_reason": error_msg,
                },
            }
            DEEP_ANALYSIS_TOTAL.labels(status="failed", engine=rec.get("engine", "openclaw")).inc()
            notify_dict = {**rec, **update}
            await _notify_feishu_deep_analysis_failed(notify_dict, error_msg, policy=policy)
            return {"id": record_id, "action": "update", **update}

        # --- pending / 其他状态 → skip ---
        logger.info(
            "[Poller] 分析仍在进行中: id=%s elapsed=%.0fs status=%s",
            record_id,
            elapsed_total,
            result.get("status", "unknown"),
        )
        return {"id": record_id, "action": "skip"}

    except Exception as e:
        logger.error("轮询记录 id=%s 失败: %s", record_id, e, exc_info=True)
        return {
            "id": record_id,
            "action": "update",
            "status": DeepAnalysisStatus.FAILED,
            "analysis_result": {
                "root_cause": f"分析任务崩溃: {e}",
                "error": str(e),
                "failure_reason": f"轮询异常: {e}",
            },
        }


def _record_to_poll_dict(record: Any) -> WebhookData:
    return {
        "id": record.id,
        "webhook_event_id": record.webhook_event_id,
        "engine": record.engine,
        "openclaw_session_key": record.openclaw_session_key,
        "openclaw_run_id": record.openclaw_run_id,
        "created_at": record.created_at,
        "status": record.status,
        "analysis_result": record.analysis_result,
        "duration_seconds": record.duration_seconds,
        "poll_attempts": record.poll_attempts,
        "last_polled_at": record.last_polled_at,
    }


async def _claim_openclaw_poll(
    analysis_id: int, *, policy: OpenClawPollPolicy | None = None
) -> tuple[WebhookData | None, int | None]:
    """Atomically claim a due pending analysis and hide it from scanner fallback."""
    from sqlalchemy import select, update

    from db.session import session_scope
    from models import DeepAnalysis

    policy = policy or OpenClawPollPolicy.from_config()
    now = datetime.now()
    lease_until = now + timedelta(seconds=_poll_claim_lease_seconds(policy))
    async with session_scope() as session:
        result = await session.execute(
            update(DeepAnalysis)
            .where(DeepAnalysis.id == analysis_id)
            .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            .where((DeepAnalysis.next_poll_at.is_(None)) | (DeepAnalysis.next_poll_at <= now))
            .values(
                poll_attempts=DeepAnalysis.poll_attempts + 1,
                last_polled_at=now,
                next_poll_at=lease_until,
            )
            .returning(DeepAnalysis)
        )
        record = result.scalar_one_or_none()
        if record:
            return _record_to_poll_dict(record), None

        next_poll_at = (
            await session.execute(
                select(DeepAnalysis.next_poll_at)
                .where(DeepAnalysis.id == analysis_id)
                .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            )
        ).scalar_one_or_none()
        if next_poll_at and next_poll_at > now:
            return None, _seconds_until(next_poll_at)
    return None, None


async def poll_deep_analysis_once(analysis_id: int, *, policy: OpenClawPollPolicy | None = None) -> None:
    """Poll one pending DeepAnalysis record and reschedule if it is still pending."""
    from db.session import session_scope
    from models import DeepAnalysis

    try:
        policy = policy or OpenClawPollPolicy.from_config()
        record_dict, early_reschedule_delay = await _claim_openclaw_poll(analysis_id, policy=policy)
        if early_reschedule_delay is not None:
            logger.debug(
                "[Poller] OpenClaw poll 任务提前触发，重新调度: id=%s delay=%ss",
                analysis_id,
                early_reschedule_delay,
            )
            await _schedule_openclaw_poll_task(analysis_id, early_reschedule_delay)
            return
        if record_dict is None:
            logger.debug("[Poller] 没有可领取的 pending 分析: id=%s", analysis_id)
            return

        logger.info(
            "[Poller] 轮询 OpenClaw 分析: id=%s webhook_id=%s attempt=%s",
            analysis_id,
            record_dict.get("webhook_event_id"),
            record_dict.get("poll_attempts"),
        )

        poll_result = await _poll_single_record(record_dict, policy=policy)

        if poll_result.get("action") != "update":
            await _schedule_next_openclaw_poll(
                analysis_id,
                int(record_dict.get("poll_attempts") or 0),
                _poll_timeout_started_at(record_dict),
                policy=policy,
            )
            return

        async with session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(DeepAnalysis)
                .where(DeepAnalysis.id == analysis_id)
                .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            )
            record = result.scalar_one_or_none()
            if not record:
                return

            if "status" in poll_result:
                record.status = poll_result["status"]
            if "analysis_result" in poll_result:
                record.analysis_result = poll_result["analysis_result"]
            if "duration_seconds" in poll_result:
                record.duration_seconds = poll_result["duration_seconds"]
            record.next_poll_at = None

            await session.flush()

            if poll_result.get("_need_success_notify"):
                try:
                    from models import WebhookEvent

                    evt_stmt = select(WebhookEvent).filter_by(id=record_dict["webhook_event_id"])
                    evt_result = await session.execute(evt_stmt)
                    event = evt_result.scalars().first()
                    source = event.source if event else ""
                    notify_dict = {**record_dict, **poll_result}
                    asyncio.create_task(_safe_notify(_notify_feishu_deep_analysis(notify_dict, source, policy=policy)))
                except Exception as e:
                    logger.debug("飞书深度分析通知失败: %s", e)

    except Exception as e:
        logger.error("[Poller] 轮询任务异常 analysis_id=%s error=%s", analysis_id, e, exc_info=True)


async def _schedule_next_openclaw_poll(
    analysis_id: int,
    poll_attempts: int,
    created_at: datetime | None,
    *,
    policy: OpenClawPollPolicy | None = None,
) -> None:
    from db.session import session_scope
    from models import DeepAnalysis
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    delay = _clamp_poll_delay_to_timeout(
        compute_openclaw_poll_delay(poll_attempts, policy=policy), created_at, policy=policy
    )
    next_poll_at = datetime.now() + timedelta(seconds=delay)
    async with session_scope() as session:
        record = await session.get(DeepAnalysis, analysis_id)
        if not record or record.status != DeepAnalysisStatus.PENDING:
            return
        record.next_poll_at = next_poll_at

    await _schedule_openclaw_poll_task(analysis_id, delay)
    logger.info("[Poller] 已调度下次 OpenClaw 轮询 analysis_id=%s delay=%ss", analysis_id, delay)


async def _schedule_openclaw_poll_task(analysis_id: int, delay_seconds: int) -> None:
    try:
        from services.operations.taskiq_retry_scheduler import schedule_openclaw_poll

        await schedule_openclaw_poll(analysis_id, delay_seconds)
    except Exception as e:
        logger.warning("[Poller] OpenClaw 下次轮询调度失败 analysis_id=%s error=%s", analysis_id, e)


async def run_openclaw_poll_scan(limit: int = 100) -> int:
    """Schedule due OpenClaw poll records from durable DB state."""
    from sqlalchemy import select

    from db.session import session_scope
    from models import DeepAnalysis

    now = datetime.now()
    async with session_scope() as session:
        stmt = (
            select(DeepAnalysis.id)
            .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            .where((DeepAnalysis.next_poll_at.is_(None)) | (DeepAnalysis.next_poll_at <= now))
            .order_by(DeepAnalysis.next_poll_at.asc(), DeepAnalysis.id.asc())
            .limit(limit)
        )
        ids = list((await session.execute(stmt)).scalars().all())

    for analysis_id in ids:
        await _schedule_openclaw_poll_task(analysis_id, 0)
    if ids:
        logger.info("[Poller] 扫描调度 pending OpenClaw 分析 count=%s ids=%s", len(ids), ids)
    else:
        logger.debug("[Poller] 扫描未发现待调度 OpenClaw 分析")
    return len(ids)


def _extract_robust_json(text: str) -> str | None:
    """Compatibility wrapper for the old parser helper name."""
    return extract_robust_json(text)
