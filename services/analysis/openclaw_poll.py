"""OpenClaw polling state machine and persistence."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from contracts.webhook_payload import JsonObject
from core import json
from core.datetime_utils import parse_utc_datetime, utcnow
from core.http_client import get_http_client
from core.json import extract_balanced_json_text
from core.logger import get_logger
from core.observability.metrics import DEEP_ANALYSIS_TOTAL
from core.observability.tracing import get_current_trace_id
from core.redis_client import redis_delete, redis_get_json_dict, redis_setex_json
from core.redis_health import openclaw_poller_stability
from db.session import session_scope
from models import DeepAnalysis, WebhookEvent
from services.analysis.openclaw_client import (
    OpenClawPollPolicy,
    poll_openclaw_final,
    poll_session_result,
)
from services.operations.deep_analysis_notifications import (
    EVENT_IMPORTANCE_KEY,
    EVENT_IS_DUPLICATE_KEY,
    EVENT_PARSED_DATA_KEY,
    send_deep_analysis_failure_notification,
    send_deep_analysis_success_notification,
)
from services.operations.taskiq_retry_scheduler import (
    compute_openclaw_poll_delay,
    schedule_openclaw_poll,
)
from services.webhooks.types import (
    MANUAL_RETRY_STARTED_AT,
    OPENCLAW_NEED_SUCCESS_NOTIFY,
    OPENCLAW_RUN_ID,
    OPENCLAW_TEXT,
    DeepAnalysisStatus,
)

logger = get_logger("openclaw.poll")
MANUAL_RETRY_STARTED_AT_KEY = MANUAL_RETRY_STARTED_AT


async def _safe_notify(coro: Any) -> None:
    try:
        await coro
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        logger.warning("[Poller] 后台通知失败: %s", e)


def _seconds_until(target: datetime) -> int:
    return max(1, int((target - utcnow()).total_seconds()))


def _clamp_poll_delay_to_timeout(
    delay_seconds: int, created_at: datetime | None, *, policy: OpenClawPollPolicy | None = None
) -> int:
    return (policy or OpenClawPollPolicy.from_config()).clamp_delay_to_timeout(delay_seconds, created_at)


def _poll_claim_lease_seconds(policy: OpenClawPollPolicy | None = None) -> int:
    return (policy or OpenClawPollPolicy.from_config()).poll_claim_lease_seconds


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _poll_timeout_started_at(rec: JsonObject) -> datetime | None:
    analysis_result = rec.get("analysis_result")
    if isinstance(analysis_result, dict):
        manual_retry_started_at = analysis_result.get(MANUAL_RETRY_STARTED_AT_KEY)
        if isinstance(manual_retry_started_at, str) and manual_retry_started_at:
            parsed = parse_utc_datetime(manual_retry_started_at)
            if parsed is not None:
                return parsed
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


async def _get_poll_stability(record_id: int) -> JsonObject | None:
    return await redis_get_json_dict(openclaw_poller_stability(record_id))


async def _set_poll_stability(record_id: int, data: JsonObject, *, policy: OpenClawPollPolicy) -> None:
    await redis_setex_json(openclaw_poller_stability(record_id), policy.stability_ttl_seconds, data)


async def _clear_poll_stability(record_id: int) -> None:
    await redis_delete(openclaw_poller_stability(record_id))


async def clear_openclaw_poll_state(record_id: int) -> None:
    await _clear_poll_stability(record_id)


async def poll_openclaw_result_via_http(
    session_key: str,
    retry_count: int = 3,
    *,
    policy: OpenClawPollPolicy | None = None,
    http_client: Any | None = None,
) -> JsonObject:
    policy = policy or OpenClawPollPolicy.from_config()
    return await poll_openclaw_final(
        session_key,
        policy=policy,
        http_client=http_client or get_http_client(),
        trace_id=get_current_trace_id(),
        retry_count=retry_count,
    )


def _poll_update(record_id: int, **fields: Any) -> JsonObject:
    return {"id": record_id, "action": "update", **fields}


def _poll_skip(record_id: int) -> JsonObject:
    return {"id": record_id, "action": "skip"}


def _elapsed_since(started_at: datetime | None, *, default: float = 0.0) -> float:
    return (utcnow() - started_at).total_seconds() if started_at else default


async def _failure_update_with_notification(
    rec: JsonObject,
    update: JsonObject,
    reason: str,
    *,
    policy: OpenClawPollPolicy,
) -> JsonObject:
    record_id = rec["id"]
    await _clear_poll_stability(record_id)
    notify_dict = {**rec, **update}
    await send_deep_analysis_failure_notification(notify_dict, reason, policy=policy)
    return _poll_update(record_id, **update)


async def _handle_poll_timeout(
    rec: JsonObject,
    timeout_started_at: datetime | None,
    *,
    policy: OpenClawPollPolicy,
) -> JsonObject | None:
    if timeout_started_at is None:
        return None
    record_id = rec["id"]
    elapsed_total = _elapsed_since(timeout_started_at)
    timeout_seconds = policy.timeout_seconds
    if elapsed_total <= timeout_seconds:
        return None
    logger.info("[Poller] 分析超时: id=%s elapsed=%.0fs timeout=%ss", record_id, elapsed_total, timeout_seconds)
    DEEP_ANALYSIS_TOTAL.labels(status="timeout", engine=rec.get("engine", "openclaw")).inc()
    update: JsonObject = {"status": DeepAnalysisStatus.FAILED, "analysis_result": {"root_cause": "OpenClaw 分析超时"}}
    return await _failure_update_with_notification(rec, update, "超时失败", policy=policy)


async def _handle_missing_session_key(
    rec: JsonObject,
    timeout_started_at: datetime | None,
    *,
    policy: OpenClawPollPolicy,
) -> JsonObject | None:
    if rec["openclaw_session_key"]:
        return None

    record_id = rec["id"]
    elapsed = _elapsed_since(timeout_started_at, default=float(policy.timeout_seconds))
    if elapsed < compute_openclaw_poll_delay(0, policy=policy):
        return _poll_skip(record_id)
    logger.warning("[Poller] 缺少 session_key，标记失败: id=%s elapsed=%.0fs", record_id, elapsed)
    DEEP_ANALYSIS_TOTAL.labels(status="failed", engine=rec.get("engine", "openclaw")).inc()
    update: JsonObject = {
        "status": DeepAnalysisStatus.FAILED,
        "analysis_result": {
            "root_cause": "无法获取分析会话，OpenClaw 触发失败",
            "error": "missing_session_key",
            "failure_reason": "未能获取到分析会话密钥",
        },
    }
    return await _failure_update_with_notification(rec, update, "无 session_key - OpenClaw 触发失败", policy=policy)


async def _fetch_poll_result(rec: JsonObject, *, policy: OpenClawPollPolicy) -> JsonObject:
    if policy.has_http_api:
        return await poll_openclaw_result_via_http(rec["openclaw_session_key"], policy=policy)
    return await poll_session_result(
        gateway_url=policy.gateway_url,
        gateway_token=policy.gateway_token,
        session_key=rec["openclaw_session_key"],
        timeout=policy.poll_timeout_seconds,
    )


def extract_robust_json(text: str) -> str | None:
    return extract_balanced_json_text(text, allow_arrays=False)


def _parse_openclaw_payload(text: str) -> dict[str, Any] | None:
    """Best-effort structured parse of OpenClaw text.

    Uses the same robust pipeline as the report normalizer so that a leading
    "thinking" prose preamble, trailing text, markdown fences, escaped JSON and
    *truncated* JSON are all recovered into a mapping instead of collapsing the
    whole raw blob into ``root_cause``. Falls back to a plain ``json.loads`` on
    the first balanced object, then to ``None`` when nothing parses.
    """
    # Imported lazily to avoid a heavy import (json_repair) on module load and
    # to keep the contracts -> analysis dependency one-directional at runtime.
    from contracts.deep_analysis_report import parse_openclaw_report_payload

    parsed = parse_openclaw_report_payload(text)
    if isinstance(parsed, dict):
        return parsed

    json_text = extract_robust_json(text)
    if json_text:
        try:
            loaded = json.loads(json_text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            return loaded
    return None


def build_analysis_result_from_openclaw_text(text: str, run_id: str = "") -> JsonObject:
    parsed_result = _parse_openclaw_payload(text)
    if parsed_result is not None:
        parsed_result[OPENCLAW_RUN_ID] = run_id
        parsed_result[OPENCLAW_TEXT] = text
        return dict(parsed_result)
    # Nothing parsed as structured JSON. This is now reached only for genuinely
    # unstructured text (plain prose / degraded fallback), NOT for the
    # "thinking prefix + (possibly truncated) JSON" blobs that previously
    # collapsed here — those are recovered by _parse_openclaw_payload above.
    # For real prose the text itself is the best available signal, so surface it
    # as root_cause for display.
    return {"root_cause": text, OPENCLAW_TEXT: text}


def _completed_update(rec: JsonObject, text: str, timeout_started_at: datetime | None) -> JsonObject:
    record_id = rec["id"]
    analysis_result = build_analysis_result_from_openclaw_text(text, str(rec["openclaw_run_id"] or ""))
    duration = _elapsed_since(timeout_started_at)
    DEEP_ANALYSIS_TOTAL.labels(status="completed", engine=rec.get("engine", "openclaw")).inc()
    return _poll_update(
        record_id,
        **{OPENCLAW_NEED_SUCCESS_NOTIFY: True},
        status=DeepAnalysisStatus.COMPLETED,
        analysis_result=analysis_result,
        duration_seconds=duration,
    )


def _poll_snapshot(text: str, msg_count: int) -> JsonObject:
    return {"msg_count": msg_count, "text_len": len(text), "text_hash": _text_hash(text)}


def _is_same_poll_snapshot(previous: JsonObject | None, current: JsonObject) -> bool:
    return bool(
        previous
        and previous.get("msg_count") == current["msg_count"]
        and previous.get("text_len") == current["text_len"]
        and previous.get("text_hash") == current["text_hash"]
    )


async def _handle_completed_poll_result(
    rec: JsonObject,
    result: JsonObject,
    timeout_started_at: datetime | None,
    *,
    policy: OpenClawPollPolicy,
) -> JsonObject:
    record_id = rec["id"]
    text = str(result.get("text", ""))
    msg_count = int(result.get("msg_count", 0) or 0)
    required_hits = 1 if result.get("is_final") is True else policy.stability_required_hits

    if required_hits <= 1:
        logger.info("[Poller] 分析完成，稳定命中阈值为 1，直接写库: id=%s", record_id)
        await _clear_poll_stability(record_id)
        return _completed_update(rec, text, timeout_started_at)

    current_snapshot = _poll_snapshot(text, msg_count)
    prev_snapshot = await _get_poll_stability(record_id)

    if _is_same_poll_snapshot(prev_snapshot, current_snapshot):
        hit_count = int(prev_snapshot.get("hit_count", 1) if prev_snapshot else 1) + 1
        logger.info(
            "[Poller] 结果稳定检查: id=%s hit=%s/%s msg_count=%s text_len=%s",
            record_id,
            hit_count,
            required_hits,
            msg_count,
            len(text),
        )
        if hit_count < required_hits:
            await _set_poll_stability(record_id, {**current_snapshot, "hit_count": hit_count}, policy=policy)
            return _poll_skip(record_id)
        logger.info("[Poller] 分析稳定确认，准备写库: id=%s", record_id)
        await _clear_poll_stability(record_id)
        return _completed_update(rec, text, timeout_started_at)

    logger.info("[Poller] 首次或结果变化，等待稳定: id=%s msg_count=%s text_len=%s", record_id, msg_count, len(text))
    await _set_poll_stability(
        record_id, {**current_snapshot, "hit_count": 1, "first_result": {"text": text}}, policy=policy
    )
    return _poll_skip(record_id)


async def _handle_error_poll_result(
    rec: JsonObject,
    result: JsonObject,
    *,
    policy: OpenClawPollPolicy,
) -> JsonObject:
    record_id = rec["id"]
    prev_snapshot = await _get_poll_stability(record_id)
    if prev_snapshot and "first_result" in prev_snapshot:
        error_count = int(prev_snapshot.get("error_count", 0) or 0) + 1
        if error_count >= policy.max_consecutive_errors and policy.enable_degradation:
            first_result = prev_snapshot.get("first_result", {})
            text = str(first_result.get("text", "")) if isinstance(first_result, dict) else ""
            logger.warning("[Poller] 连续错误达阈值，降级使用首次结果: id=%s error_count=%d", record_id, error_count)
            await _clear_poll_stability(record_id)
            DEEP_ANALYSIS_TOTAL.labels(status="degraded", engine=rec.get("engine", "openclaw")).inc()
            return _poll_update(
                record_id,
                status=DeepAnalysisStatus.COMPLETED,
                analysis_result=build_analysis_result_from_openclaw_text(text, str(rec["openclaw_run_id"] or "")),
            )
        await _set_poll_stability(record_id, {**prev_snapshot, "error_count": error_count}, policy=policy)
        return _poll_skip(record_id)

    error_msg = str(result.get("error", "OpenClaw 返回错误"))
    if bool(result.get("retryable")) or _is_transient_poll_error(error_msg):
        logger.warning(
            "[Poller] OpenClaw 轮询遇到临时错误，保留 pending 等待下轮重试: id=%s error=%s", record_id, error_msg
        )
        return _poll_skip(record_id)

    DEEP_ANALYSIS_TOTAL.labels(status="failed", engine=rec.get("engine", "openclaw")).inc()
    update: JsonObject = {
        "status": DeepAnalysisStatus.FAILED,
        "analysis_result": {"root_cause": error_msg, "error": error_msg, "failure_reason": error_msg},
    }
    return await _failure_update_with_notification(rec, update, error_msg, policy=policy)


async def _handle_poll_result(
    rec: JsonObject,
    result: JsonObject,
    timeout_started_at: datetime | None,
    *,
    policy: OpenClawPollPolicy,
) -> JsonObject:
    status = result.get("status")
    if status == "completed":
        return await _handle_completed_poll_result(rec, result, timeout_started_at, policy=policy)
    if status == "error":
        return await _handle_error_poll_result(rec, result, policy=policy)
    logger.info(
        "[Poller] 分析仍在进行中: id=%s elapsed=%.0fs status=%s",
        rec["id"],
        _elapsed_since(timeout_started_at),
        status or "unknown",
    )
    return _poll_skip(rec["id"])


async def _poll_single_record(rec: JsonObject, *, policy: OpenClawPollPolicy | None = None) -> JsonObject:
    policy = policy or OpenClawPollPolicy.from_config()
    record_id = rec["id"]

    try:
        timeout_started_at = _poll_timeout_started_at(rec)
        timeout_result = await _handle_poll_timeout(rec, timeout_started_at, policy=policy)
        if timeout_result is not None:
            return timeout_result
        missing_session_result = await _handle_missing_session_key(rec, timeout_started_at, policy=policy)
        if missing_session_result is not None:
            return missing_session_result
        result = await _fetch_poll_result(rec, policy=policy)
        return await _handle_poll_result(rec, result, timeout_started_at, policy=policy)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
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


def _record_to_poll_dict(record: Any) -> JsonObject:
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
) -> tuple[JsonObject | None, int | None]:
    policy = policy or OpenClawPollPolicy.from_config()
    now = utcnow()
    lease_until = now + timedelta(seconds=_poll_claim_lease_seconds(policy))
    async with session_scope() as session:
        result = await session.execute(
            update(DeepAnalysis)
            .where(DeepAnalysis.id == analysis_id)
            .where(DeepAnalysis.status == DeepAnalysisStatus.PENDING)
            .where((DeepAnalysis.next_poll_at.is_(None)) | (DeepAnalysis.next_poll_at <= now))
            .values(poll_attempts=DeepAnalysis.poll_attempts + 1, last_polled_at=now, next_poll_at=lease_until)
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


async def _schedule_openclaw_poll_task(analysis_id: int, delay_seconds: int) -> None:
    try:
        await schedule_openclaw_poll(analysis_id, delay_seconds)
    except (OSError, RuntimeError, TimeoutError) as e:
        logger.warning("[Poller] OpenClaw 下次轮询调度失败 analysis_id=%s error=%s", analysis_id, e)


async def _schedule_next_openclaw_poll(
    analysis_id: int,
    poll_attempts: int,
    created_at: datetime | None,
    *,
    policy: OpenClawPollPolicy | None = None,
) -> None:
    delay = _clamp_poll_delay_to_timeout(
        compute_openclaw_poll_delay(poll_attempts, policy=policy), created_at, policy=policy
    )
    next_poll_at = utcnow() + timedelta(seconds=delay)
    async with session_scope() as session:
        record = await session.get(DeepAnalysis, analysis_id)
        if not record or record.status != DeepAnalysisStatus.PENDING:
            return
        record.next_poll_at = next_poll_at
    await _schedule_openclaw_poll_task(analysis_id, delay)
    logger.info("[Poller] 已调度下次 OpenClaw 轮询 analysis_id=%s delay=%ss", analysis_id, delay)


async def poll_deep_analysis_once(analysis_id: int, *, policy: OpenClawPollPolicy | None = None) -> None:
    try:
        policy = policy or OpenClawPollPolicy.from_config()
        record_dict, early_reschedule_delay = await _claim_openclaw_poll(analysis_id, policy=policy)
        if early_reschedule_delay is not None:
            logger.debug(
                "[Poller] OpenClaw poll 任务提前触发，重新调度: id=%s delay=%ss", analysis_id, early_reschedule_delay
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

        notify_payload: tuple[dict[str, Any], str] | None = None
        async with session_scope() as session:
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

            if poll_result.get(OPENCLAW_NEED_SUCCESS_NOTIFY):
                # Collect notification data inside the session (detached plain
                # dicts), but send it AFTER the transaction commits so a slow
                # notification cannot hold the DB transaction open.
                evt_stmt = select(WebhookEvent).filter_by(id=record_dict["webhook_event_id"])
                evt_result = await session.execute(evt_stmt)
                event = evt_result.scalars().first()
                source = event.source if event else ""
                notify_dict = {**record_dict, **poll_result}
                if event:
                    notify_dict[EVENT_IMPORTANCE_KEY] = str(event.importance or "")
                    notify_dict[EVENT_IS_DUPLICATE_KEY] = bool(event.is_duplicate)
                    notify_dict[EVENT_PARSED_DATA_KEY] = dict(event.parsed_data or {})
                notify_payload = (notify_dict, source)

        # Awaited (not fire-and-forget): a bare create_task here can be
        # garbage-collected or dropped on worker shutdown, silently losing the
        # success notification. _safe_notify swallows delivery errors so this
        # cannot fail the poll.
        if notify_payload is not None:
            notify_dict, source = notify_payload
            await _safe_notify(send_deep_analysis_success_notification(notify_dict, source, policy=policy))
    except (OSError, RuntimeError, SQLAlchemyError, ValueError) as e:
        logger.error("[Poller] 轮询任务异常 analysis_id=%s error=%s", analysis_id, e, exc_info=True)


async def run_openclaw_poll_scan(limit: int = 100) -> int:
    now = utcnow()
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
