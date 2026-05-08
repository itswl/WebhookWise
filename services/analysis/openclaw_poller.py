"""OpenClaw analysis result polling.

Each pending DeepAnalysis record schedules its own TaskIQ one-shot poll. The DB
stores audit state, while TaskIQ owns retry/poll timing.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from core.config import Config
from core.http_client import get_http_client
from core.metrics import DEEP_ANALYSIS_TOTAL
from core.trace import get_trace_id

logger = logging.getLogger("webhook_service.openclaw_poller")

WebhookData = dict[str, Any]


def _seconds_until(target: datetime) -> int:
    return max(1, int((target - datetime.now()).total_seconds()))


def _clamp_poll_delay_to_timeout(delay_seconds: int, created_at: datetime | None) -> int:
    if created_at is None:
        return delay_seconds
    elapsed = (datetime.now() - created_at).total_seconds()
    remaining = int(Config.openclaw.OPENCLAW_TIMEOUT_SECONDS - elapsed)
    if remaining <= 0:
        return 1
    return max(1, min(delay_seconds, remaining))


async def _get_poll_stability(record_id: int) -> WebhookData | None:
    from core.redis_client import redis_get_json_dict

    return await redis_get_json_dict(f"openclaw:poller:stability:{record_id}")


async def _set_poll_stability(record_id: int, data: WebhookData) -> None:
    from core.redis_client import redis_setex_json

    await redis_setex_json(f"openclaw:poller:stability:{record_id}", 3600, data)


async def _clear_poll_stability(record_id: int) -> None:
    from core.redis_client import redis_delete

    await redis_delete(f"openclaw:poller:stability:{record_id}")


async def _notify_feishu_deep_analysis(record_dict: WebhookData, source: str = "") -> None:
    """发送深度分析完成的飞书通知（接受 dict）"""
    from adapters.ecosystem_adapters import send_feishu_deep_analysis
    from core.config import Config

    webhook_url = Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK
    if not webhook_url:
        return

    try:
        analysis_data = {
            "analysis_result": record_dict["analysis_result"],
            "engine": record_dict["engine"],
            "duration_seconds": record_dict.get("duration_seconds") or 0,
        }
        success = await send_feishu_deep_analysis(
            webhook_url=webhook_url,
            analysis_record=analysis_data,
            source=source,
            webhook_event_id=record_dict["webhook_event_id"],
        )
        if not success:
            try:
                from services.forwarding.forward import record_failed_forward

                await record_failed_forward(
                    webhook_event_id=record_dict["webhook_event_id"],
                    forward_rule_id=None,
                    target_url=webhook_url,
                    target_type="feishu",
                    failure_reason="feishu_notification_failed",
                    error_message="深度分析飞书通知发送失败",
                    forward_data={
                        "webhook_event_id": record_dict["webhook_event_id"],
                        "analysis_type": "deep_analysis",
                    },
                )
            except Exception as rec_err:
                logger.warning(f"记录飞书通知失败异常: {rec_err}")
        else:
            logger.info(
                "[Poller] 飞书深度分析通知已发送: id=%s event_id=%s",
                record_dict.get("id"),
                record_dict["webhook_event_id"],
            )
    except Exception as e:
        logger.warning(f"飞书深度分析通知失败: {e}")


def build_analysis_result_from_openclaw_text(text: str, run_id: str = "") -> WebhookData:
    """将 OpenClaw 原文转换为可持久化的 analysis_result。"""
    parsed_result = None
    json_text = _extract_robust_json(text)
    if json_text:
        try:
            parsed_result = json.loads(json_text)
        except Exception:
            parsed_result = None

    if parsed_result and isinstance(parsed_result, dict):
        parsed_result["_openclaw_run_id"] = run_id
        parsed_result["_openclaw_text"] = text
        return dict(parsed_result)
    return {"root_cause": text, "_openclaw_text": text}


async def notify_deep_analysis_success(record: Any, source: str = "") -> None:
    record_dict = {
        "id": record.id,
        "webhook_event_id": record.webhook_event_id,
        "engine": record.engine,
        "analysis_result": record.analysis_result,
        "duration_seconds": record.duration_seconds,
    }
    await _notify_feishu_deep_analysis(record_dict, source)


async def _notify_feishu_deep_analysis_failed(record_dict: WebhookData, reason: str = "") -> None:
    """发送深度分析失败的飞书通知（接受 dict）"""
    from adapters.ecosystem_adapters import send_feishu_deep_analysis
    from core.config import Config

    webhook_url = Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK
    if not webhook_url:
        return

    try:
        # 构建失败结果
        analysis_result = record_dict.get("analysis_result")
        failed_result = analysis_result.copy() if analysis_result else {}
        failed_result["analysis_failed"] = True
        failed_result["failure_reason"] = reason

        analysis_data = {
            "analysis_result": failed_result,
            "engine": record_dict["engine"],
            "duration_seconds": record_dict.get("duration_seconds") or 0,
        }
        success = await send_feishu_deep_analysis(
            webhook_url=webhook_url,
            analysis_record=analysis_data,
            source="",
            webhook_event_id=record_dict["webhook_event_id"],
        )
        if success:
            logger.info(f"深度分析失败通知已发送: id={record_dict['id']}, reason={reason}")
        else:
            try:
                from services.forwarding.forward import record_failed_forward

                await record_failed_forward(
                    webhook_event_id=record_dict["webhook_event_id"],
                    forward_rule_id=None,
                    target_url=webhook_url,
                    target_type="feishu",
                    failure_reason="feishu_failure_notification_failed",
                    error_message=f"深度分析失败飞书通知发送失败: {reason}",
                    forward_data={
                        "webhook_event_id": record_dict["webhook_event_id"],
                        "analysis_type": "deep_analysis_failed",
                    },
                )
            except Exception as rec_err:
                logger.warning(f"记录飞书通知失败异常: {rec_err}")
    except Exception as e:
        logger.warning(f"飞书深度分析失败通知失败: {e}")


async def _poll_via_http(session_key: str, retry_count: int = 3) -> WebhookData:
    """
    通过 HTTP API /final 接口获取分析结果（带重试）

    使用全局 httpx.AsyncClient 单例，复用连接池。

    Returns:
        - 成功: {"status": "completed", "text": "...", "msg_count": N}
        - 暂无结果: {"status": "pending"}
        - 错误: {"status": "error", "error": "..."}
    """
    base_url = Config.openclaw.OPENCLAW_HTTP_API_URL.rstrip("/")
    last_error = None

    # 使用 hooks token 认证
    hooks_token = Config.openclaw.OPENCLAW_HOOKS_TOKEN or Config.openclaw.OPENCLAW_GATEWAY_TOKEN
    headers = {"Authorization": f"Bearer {hooks_token}"}
    trace_id = get_trace_id()
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    client = get_http_client()
    for attempt in range(retry_count):
        try:
            # 使用 /final 接口直接获取最终结果
            url = f"{base_url}/sessions/{session_key}/final"
            logger.debug("HTTP /final 请求 (尝试 %s/%s): %s", attempt + 1, retry_count, url)

            response = await client.get(url, headers=headers, timeout=30.0)

            if response.status_code == 404:
                last_error = "Session not found"
                logger.warning(f"Session 未找到 (尝试 {attempt + 1}/{retry_count})")
                continue

            if response.status_code == 204 or response.status_code == 202:
                # 204 No Content / 202 Accepted - 分析仍在进行中
                last_error = "分析进行中"
                logger.debug("分析进行中 (尝试 %s/%s)", attempt + 1, retry_count)
                continue

            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue

            raw = response.json()
            if not isinstance(raw, dict):
                last_error = "Invalid JSON response"
                continue
            data: WebhookData = raw

            # 根据 /final 接口返回的字段判断状态
            is_final = data.get("isFinal", False)
            is_processing = data.get("isProcessing", False)
            text = data.get("text", "")
            msg_count = int(data.get("messageCount", 0) or 0)

            # 判断是否完成
            if is_processing and not text:
                last_error = "分析进行中"
                continue

            if text:
                return {"status": "completed", "text": text, "msg_count": msg_count}

            if not is_final:
                last_error = "分析进行中"
                continue

            last_error = "No text content"
            continue

        except Exception as e:
            last_error = str(e)
            logger.warning(f"HTTP 轮询异常: {e}")

    if last_error == "分析进行中":
        return {"status": "pending"}
    return {"status": "error", "error": last_error}


async def _poll_single_record(rec: WebhookData, semaphore: asyncio.Semaphore) -> WebhookData:
    """对单条 pending 记录执行 HTTP 轮询 + 稳定性检查（完全脱离 DB）。

    返回一个 dict 描述本次轮询的处理结果，供阶段 3 写回 DB。
    返回格式::

        {"id": int, "action": "skip" | "update", ...更新字段}
    """
    from core.config import Config
    from services.analysis.openclaw_ws_client import poll_session_result
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    record_id = rec["id"]

    async with semaphore:
        try:
            created_at = rec.get("created_at")
            created_dt = created_at if isinstance(created_at, datetime) else None
            # --- 超时检查 ---
            timeout_seconds = Config.openclaw.OPENCLAW_TIMEOUT_SECONDS
            elapsed_total = (datetime.now() - created_dt).total_seconds() if created_dt else 0.0
            if created_dt and elapsed_total > timeout_seconds:
                logger.info(
                    "[Poller] 分析超时: id=%s elapsed=%.0fs timeout=%ss", record_id, elapsed_total, timeout_seconds
                )
                await _clear_poll_stability(record_id)
                DEEP_ANALYSIS_TOTAL.labels(status="timeout", engine=rec.get("engine", "openclaw")).inc()
                update: WebhookData = {
                    "status": "failed",
                    "analysis_result": {"root_cause": "OpenClaw 分析超时"},
                }
                notify_dict = {**rec, **update}
                await _notify_feishu_deep_analysis_failed(notify_dict, "超时失败")
                return {"id": record_id, "action": "update", **update}

            # --- session_key 缺失检查 ---
            if not rec["openclaw_session_key"]:
                elapsed = (datetime.now() - created_dt).total_seconds() if created_dt else 999.0
                if elapsed < compute_openclaw_poll_delay(0):
                    return {"id": record_id, "action": "skip"}
                logger.warning("[Poller] 缺少 session_key，标记失败: id=%s elapsed=%.0fs", record_id, elapsed)
                DEEP_ANALYSIS_TOTAL.labels(status="failed", engine=rec.get("engine", "openclaw")).inc()
                update = {
                    "status": "failed",
                    "analysis_result": {
                        "root_cause": "无法获取分析会话，OpenClaw 触发失败",
                        "error": "missing_session_key",
                        "failure_reason": "未能获取到分析会话密钥",
                    },
                }
                await _clear_poll_stability(record_id)
                notify_dict = {**rec, **update}
                await _notify_feishu_deep_analysis_failed(notify_dict, "无 session_key - OpenClaw 触发失败")
                return {"id": record_id, "action": "update", **update}

            # --- HTTP 轮询 ---
            if Config.openclaw.OPENCLAW_HTTP_API_URL:
                result = await _poll_via_http(rec["openclaw_session_key"])
            else:
                result = await poll_session_result(
                    gateway_url=Config.openclaw.OPENCLAW_GATEWAY_URL,
                    gateway_token=Config.openclaw.OPENCLAW_GATEWAY_TOKEN,
                    session_key=rec["openclaw_session_key"],
                    timeout=Config.openclaw.OPENCLAW_POLL_TIMEOUT,
                )

            # --- 处理 completed ---
            if result.get("status") == "completed":
                text = result.get("text", "")
                msg_count = int(result.get("msg_count", 0) or 0)

                current_snapshot = {"msg_count": msg_count, "text_len": len(text)}
                prev_snapshot = await _get_poll_stability(record_id)

                if (
                    prev_snapshot
                    and prev_snapshot["msg_count"] == current_snapshot["msg_count"]
                    and prev_snapshot["text_len"] == current_snapshot["text_len"]
                ):
                    hit_count = prev_snapshot.get("hit_count", 1) + 1
                    logger.info(
                        "[Poller] 结果稳定检查: id=%s hit=%s/%s msg_count=%s text_len=%s",
                        record_id,
                        hit_count,
                        Config.openclaw.OPENCLAW_STABILITY_REQUIRED_HITS,
                        msg_count,
                        len(text),
                    )
                    if hit_count >= Config.openclaw.OPENCLAW_STABILITY_REQUIRED_HITS:
                        logger.info("[Poller] 分析稳定确认，准备写库: id=%s", record_id)
                    else:
                        await _set_poll_stability(record_id, {**current_snapshot, "hit_count": hit_count})
                        return {"id": record_id, "action": "skip"}

                    await _clear_poll_stability(record_id)
                    analysis_result = build_analysis_result_from_openclaw_text(text, str(rec["openclaw_run_id"] or ""))

                    duration = (datetime.now() - created_dt).total_seconds() if created_dt else 0.0
                    update = {
                        "status": "completed",
                        "analysis_result": analysis_result,
                        "duration_seconds": duration,
                    }
                    DEEP_ANALYSIS_TOTAL.labels(status="completed", engine=rec.get("engine", "openclaw")).inc()
                    # 标记需要查 source 并发飞书通知
                    return {
                        "id": record_id,
                        "action": "update",
                        "_need_success_notify": True,
                        **update,
                    }
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
                    if (
                        error_count >= Config.openclaw.OPENCLAW_MAX_CONSECUTIVE_ERRORS
                        and Config.openclaw.OPENCLAW_ENABLE_DEGRADATION
                    ):
                        text = prev_snapshot["first_result"]["text"]
                        logger.warning(
                            "[Poller] 连续错误达阈值，降级使用首次结果: id=%s error_count=%d", record_id, error_count
                        )
                        await _clear_poll_stability(record_id)
                        DEEP_ANALYSIS_TOTAL.labels(status="degraded", engine=rec.get("engine", "openclaw")).inc()
                        return {
                            "id": record_id,
                            "action": "update",
                            "status": "completed",
                            "analysis_result": build_analysis_result_from_openclaw_text(
                                text, str(rec["openclaw_run_id"] or "")
                            ),
                        }
                    # 更新 error_count 并继续等待
                    await _set_poll_stability(record_id, {**prev_snapshot, "error_count": error_count})
                    return {"id": record_id, "action": "skip"}

                await _clear_poll_stability(record_id)
                error_msg = result.get("error", "OpenClaw 返回错误")
                update = {
                    "status": "failed",
                    "analysis_result": {
                        "root_cause": error_msg,
                        "error": error_msg,
                        "failure_reason": error_msg,
                    },
                }
                DEEP_ANALYSIS_TOTAL.labels(status="failed", engine=rec.get("engine", "openclaw")).inc()
                notify_dict = {**rec, **update}
                await _notify_feishu_deep_analysis_failed(notify_dict, error_msg)
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
            logger.error(f"轮询记录 id={record_id} 失败: {e}", exc_info=True)
            return {
                "id": record_id,
                "action": "update",
                "status": "failed",
                "analysis_result": {
                    "root_cause": f"分析任务崩溃: {e}",
                    "error": str(e),
                    "failure_reason": f"轮询异常: {e}",
                },
            }


async def poll_deep_analysis_once(analysis_id: int) -> None:
    """Poll one pending DeepAnalysis record and reschedule if it is still pending."""
    from db.session import session_scope
    from models import DeepAnalysis

    try:
        record_dict: WebhookData | None = None
        early_reschedule_delay: int | None = None
        async with session_scope() as session:
            from sqlalchemy import select
            from sqlalchemy.orm import defer

            result = await session.execute(
                select(DeepAnalysis)
                .options(defer(DeepAnalysis.user_question))
                .where(DeepAnalysis.id == analysis_id)
                .where(DeepAnalysis.status == "pending")
            )
            record = result.scalar_one_or_none()
            if not record:
                return
            now = datetime.now()
            if record.next_poll_at and record.next_poll_at > now:
                early_reschedule_delay = _seconds_until(record.next_poll_at)
                logger.debug(
                    "[Poller] OpenClaw poll 任务提前触发，重新调度: id=%s delay=%ss",
                    analysis_id,
                    early_reschedule_delay,
                )
            else:
                record.poll_attempts = (record.poll_attempts or 0) + 1
                record.last_polled_at = now
                record.next_poll_at = None
                record_dict = {
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

        if early_reschedule_delay is not None:
            await _schedule_openclaw_poll_task(analysis_id, early_reschedule_delay)
            return
        if record_dict is None:
            return

        logger.info("[Poller] 轮询 OpenClaw 分析: id=%s", analysis_id)

        semaphore = asyncio.Semaphore(5)
        poll_result = await _poll_single_record(record_dict, semaphore)

        if poll_result.get("action") != "update":
            await _schedule_next_openclaw_poll(
                analysis_id,
                int(record_dict.get("poll_attempts") or 0),
                record_dict.get("created_at") if isinstance(record_dict.get("created_at"), datetime) else None,
            )
            return

        async with session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(select(DeepAnalysis).where(DeepAnalysis.id == analysis_id))
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
                    asyncio.create_task(_notify_feishu_deep_analysis(notify_dict, source))
                except Exception as e:
                    logger.debug("飞书深度分析通知失败: %s", e)

    except Exception as e:
        logger.error("[Poller] 轮询任务异常 analysis_id=%s error=%s", analysis_id, e, exc_info=True)


async def _schedule_next_openclaw_poll(analysis_id: int, poll_attempts: int, created_at: datetime | None) -> None:
    from db.session import session_scope
    from models import DeepAnalysis
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    delay = _clamp_poll_delay_to_timeout(compute_openclaw_poll_delay(poll_attempts), created_at)
    next_poll_at = datetime.now() + timedelta(seconds=delay)
    async with session_scope() as session:
        record = await session.get(DeepAnalysis, analysis_id)
        if not record or record.status != "pending":
            return
        record.next_poll_at = next_poll_at

    await _schedule_openclaw_poll_task(analysis_id, delay)


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
            .where(DeepAnalysis.status == "pending")
            .where((DeepAnalysis.next_poll_at.is_(None)) | (DeepAnalysis.next_poll_at <= now))
            .order_by(DeepAnalysis.next_poll_at.asc(), DeepAnalysis.id.asc())
            .limit(limit)
        )
        ids = list((await session.execute(stmt)).scalars().all())

    for analysis_id in ids:
        await _schedule_openclaw_poll_task(analysis_id, 0)
    return len(ids)


def _extract_robust_json(text: str) -> str | None:
    """从文本中寻找并提取第一个完整的 JSON 对象（处理嵌套大括号）"""
    try:
        start_idx = text.find("{")
        if start_idx == -1:
            return None
        stack = 0
        for i in range(start_idx, len(text)):
            if text[i] == "{":
                stack += 1
            elif text[i] == "}":
                stack -= 1
                if stack == 0:
                    return text[start_idx : i + 1]
    except Exception:
        return None
    return None
