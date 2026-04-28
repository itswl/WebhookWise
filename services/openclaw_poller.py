"""OpenClaw 分析结果后台轮询"""

import asyncio
import json
import logging
from datetime import datetime

import core.redis_client
from core.config import Config
from core.http_client import get_http_client

logger = logging.getLogger("webhook_service.openclaw_poller")

# 轮询稳定性缓存：{analysis_id: {"msg_count": N, "text_len": M, "hit_count": int, "first_result": {...}}}
# 需要连续 N 次轮询结果一致才确认完成，避免过早提取中间结果
# 如果连续超时超过 MAX_CONSECUTIVE_ERRORS 次且已有首次结果，则降级使用首次结果
# 移除原有的内存锁和缓存字典


async def _get_poll_stability(record_id: int) -> dict:
    redis_client = core.redis_client.get_redis()
    val = await redis_client.get(f"openclaw:poller:stability:{record_id}")
    return json.loads(val) if val else None


async def _set_poll_stability(record_id: int, data: dict):
    redis_client = core.redis_client.get_redis()
    # 缓存保留 1 小时
    await redis_client.setex(f"openclaw:poller:stability:{record_id}", 3600, json.dumps(data))


async def _clear_poll_stability(record_id: int):
    redis_client = core.redis_client.get_redis()
    await redis_client.delete(f"openclaw:poller:stability:{record_id}")


async def _notify_feishu_deep_analysis(record_dict: dict, source: str = ""):
    """发送深度分析完成的飞书通知（接受 dict）"""
    from adapters.ecosystem_adapters import send_feishu_deep_analysis
    from core.config import Config

    webhook_url = Config.DEEP_ANALYSIS_FEISHU_WEBHOOK
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
                from crud.webhook import record_failed_forward

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
    except Exception as e:
        logger.warning(f"飞书深度分析通知失败: {e}")


async def _notify_feishu_deep_analysis_failed(record_dict: dict, reason: str = ""):
    """发送深度分析失败的飞书通知（接受 dict）"""
    from adapters.ecosystem_adapters import send_feishu_deep_analysis
    from core.config import Config

    webhook_url = Config.DEEP_ANALYSIS_FEISHU_WEBHOOK
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
                from crud.webhook import record_failed_forward

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


async def poll_pending_analyses():
    """查询所有 status='pending' 的 DeepAnalysis 记录，逐一轮询结果"""
    import core.redis_client

    redis_client = core.redis_client.get_redis()
    # Redis 全局分布式锁（防止多个 worker 同时查表和调接口）
    lock_key = "openclaw:poller:global_lock"

    # 尝试获取锁，有效时间 60 秒
    if not await redis_client.set(lock_key, "locked", nx=True, ex=60):
        logger.debug("另一个 worker 的轮询正在执行，跳过本轮")
        return

    try:
        await _poll_pending_analyses_inner()
    except Exception as e:
        logger.error(f"[Poller] 执行内部轮询逻辑时发生错误: {e}", exc_info=True)
    finally:
        await redis_client.delete(lock_key)


async def _poll_via_http(session_key: str, retry_count: int = 3) -> dict:
    """
    通过 HTTP API /final 接口获取分析结果（带重试）

    使用全局 httpx.AsyncClient 单例，复用连接池。

    Returns:
        - 成功: {"status": "completed", "text": "...", "msg_count": N}
        - 暂无结果: {"status": "pending"}
        - 错误: {"status": "error", "error": "..."}
    """
    base_url = Config.OPENCLAW_HTTP_API_URL.rstrip("/")
    last_error = None

    # 使用 hooks token 认证
    hooks_token = Config.OPENCLAW_HOOKS_TOKEN or Config.OPENCLAW_GATEWAY_TOKEN
    headers = {"Authorization": f"Bearer {hooks_token}"}

    client = get_http_client()
    for attempt in range(retry_count):
        try:
            # 使用 /final 接口直接获取最终结果
            url = f"{base_url}/sessions/{session_key}/final"
            logger.info(f"HTTP /final 请求 (尝试 {attempt + 1}/{retry_count}): {url}")

            response = await client.get(url, headers=headers, timeout=30.0)

            if response.status_code == 404:
                last_error = "Session not found"
                logger.warning(f"Session 未找到 (尝试 {attempt + 1}/{retry_count})")
                continue

            if response.status_code == 204 or response.status_code == 202:
                # 204 No Content / 202 Accepted - 分析仍在进行中
                last_error = "分析进行中"
                logger.debug(f"分析进行中 (尝试 {attempt + 1}/{retry_count})")
                continue

            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue

            data = response.json()

            # 根据 /final 接口返回的字段判断状态
            is_final = data.get("isFinal", False)
            is_processing = data.get("isProcessing", False)
            text = data.get("text", "")
            msg_count = data.get("messageCount", 0)

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


async def _poll_single_record(rec: dict, semaphore: "asyncio.Semaphore") -> dict:
    """对单条 pending 记录执行 HTTP 轮询 + 稳定性检查（完全脱离 DB）。

    返回一个 dict 描述本次轮询的处理结果，供阶段 3 写回 DB。
    返回格式::

        {"id": int, "action": "skip" | "update", ...更新字段}
    """
    from core.config import Config
    from services.openclaw_ws_client import poll_session_result

    record_id = rec["id"]

    async with semaphore:
        try:
            # --- 超时检查 ---
            timeout_seconds = getattr(Config, "OPENCLAW_TIMEOUT_SECONDS", 300)
            if rec["created_at"] and (datetime.now() - rec["created_at"]).total_seconds() > timeout_seconds:
                await _clear_poll_stability(record_id)
                update = {
                    "status": "failed",
                    "analysis_result": {"root_cause": "OpenClaw 分析超时"},
                }
                notify_dict = {**rec, **update}
                await _notify_feishu_deep_analysis_failed(notify_dict, "超时失败")
                return {"id": record_id, "action": "update", **update}

            # --- session_key 缺失检查 ---
            if not rec["openclaw_session_key"]:
                elapsed = (datetime.now() - rec["created_at"]).total_seconds() if rec["created_at"] else 999
                if elapsed < Config.OPENCLAW_MIN_WAIT_SECONDS:
                    return {"id": record_id, "action": "skip"}
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

            # --- 最小等待时间 ---
            elapsed = (datetime.now() - rec["created_at"]).total_seconds() if rec["created_at"] else 999
            if elapsed < Config.OPENCLAW_MIN_WAIT_SECONDS:
                return {"id": record_id, "action": "skip"}

            # --- HTTP 轮询 ---
            if Config.OPENCLAW_HTTP_API_URL:
                result = await _poll_via_http(rec["openclaw_session_key"])
            else:
                result = poll_session_result(
                    gateway_url=Config.OPENCLAW_GATEWAY_URL,
                    gateway_token=Config.OPENCLAW_GATEWAY_TOKEN,
                    session_key=rec["openclaw_session_key"],
                    timeout=Config.OPENCLAW_POLL_TIMEOUT,
                )

            # --- 处理 completed ---
            if result.get("status") == "completed":
                text = result.get("text", "")
                msg_count = result.get("msg_count", 0)

                current_snapshot = {"msg_count": msg_count, "text_len": len(text)}
                prev_snapshot = await _get_poll_stability(record_id)

                if (
                    prev_snapshot
                    and prev_snapshot["msg_count"] == current_snapshot["msg_count"]
                    and prev_snapshot["text_len"] == current_snapshot["text_len"]
                ):
                    hit_count = prev_snapshot.get("hit_count", 1) + 1
                    if hit_count >= Config.OPENCLAW_STABILITY_REQUIRED_HITS:
                        logger.debug(f"[Poller] 分析稳定确认: id={record_id}")
                    else:
                        await _set_poll_stability(record_id, {**current_snapshot, "hit_count": hit_count})
                        return {"id": record_id, "action": "skip"}

                    await _clear_poll_stability(record_id)
                    parsed_result = None
                    json_text = _extract_robust_json(text)
                    if json_text:
                        try:
                            parsed_result = json.loads(json_text)
                        except Exception:
                            parsed_result = None

                    if parsed_result and isinstance(parsed_result, dict):
                        parsed_result["_openclaw_run_id"] = rec["openclaw_run_id"]
                        parsed_result["_openclaw_text"] = text
                        analysis_result = parsed_result
                    else:
                        analysis_result = {"root_cause": text, "_openclaw_text": text}

                    duration = (datetime.now() - rec["created_at"]).total_seconds() if rec["created_at"] else 0
                    update = {
                        "status": "completed",
                        "analysis_result": analysis_result,
                        "duration_seconds": duration,
                    }
                    # 标记需要查 source 并发飞书通知
                    return {
                        "id": record_id,
                        "action": "update",
                        "_need_success_notify": True,
                        **update,
                    }
                else:
                    await _set_poll_stability(
                        record_id, {**current_snapshot, "hit_count": 1, "first_result": {"text": text}}
                    )
                    return {"id": record_id, "action": "skip"}

            # --- 处理 error ---
            elif result.get("status") == "error":
                prev_snapshot = await _get_poll_stability(record_id)
                if prev_snapshot and "first_result" in prev_snapshot:
                    error_count = prev_snapshot.get("error_count", 0) + 1
                    if error_count >= Config.OPENCLAW_MAX_CONSECUTIVE_ERRORS and Config.OPENCLAW_ENABLE_DEGRADATION:
                        text = prev_snapshot["first_result"]["text"]
                        await _clear_poll_stability(record_id)
                        parsed_result = None
                        json_text = _extract_robust_json(text)
                        if json_text:
                            try:
                                parsed_result = json.loads(json_text)
                            except Exception:
                                parsed_result = None
                        return {
                            "id": record_id,
                            "action": "update",
                            "status": "completed",
                            "analysis_result": parsed_result or {"root_cause": text},
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
                notify_dict = {**rec, **update}
                await _notify_feishu_deep_analysis_failed(notify_dict, error_msg)
                return {"id": record_id, "action": "update", **update}

            # --- pending / 其他状态 → skip ---
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


async def _poll_pending_analyses_inner():
    """轮询逻辑主体 — 三阶段分离: 查询 → 并发 HTTP → 批量更新"""
    from db.session import session_scope
    from models import DeepAnalysis

    try:
        # ── 阶段 1：查询 pending 列表（快速释放 DB 连接）──
        pending_dicts: list[dict] = []
        async with session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(DeepAnalysis).filter_by(status="pending").order_by(DeepAnalysis.created_at.asc()).limit(10)
            )
            pending = result.scalars().all()
            if not pending:
                return
            # 提取所有字段到普通 dict，避免 detached 对象问题
            pending_dicts.extend(
                {
                    "id": r.id,
                    "webhook_event_id": r.webhook_event_id,
                    "engine": r.engine,
                    "openclaw_session_key": r.openclaw_session_key,
                    "openclaw_run_id": r.openclaw_run_id,
                    "created_at": r.created_at,
                    "status": r.status,
                    "analysis_result": r.analysis_result,
                    "duration_seconds": r.duration_seconds,
                }
                for r in pending
            )
        # session_scope 结束，DB 连接已归还

        logger.info(f"[Poller] 扫描到待处理分析: count={len(pending_dicts)}")

        # ── 阶段 2：并发 HTTP 轮询（完全脱离 DB）──
        semaphore = asyncio.Semaphore(5)
        coros = [_poll_single_record(rec, semaphore) for rec in pending_dicts]
        poll_results: list[dict] = await asyncio.gather(*coros, return_exceptions=True)

        # 收集需要写回 DB 的结果
        updates: list[dict] = []
        for pr in poll_results:
            if isinstance(pr, Exception):
                logger.error(f"[Poller] 并发轮询协程异常: {pr}", exc_info=pr)
                continue
            if pr and pr.get("action") == "update":
                updates.append(pr)

        if not updates:
            return

        # ── 阶段 3：重新获取 DB 连接，批量更新 ──
        update_ids = [u["id"] for u in updates]
        update_map = {u["id"]: u for u in updates}

        async with session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(select(DeepAnalysis).filter(DeepAnalysis.id.in_(update_ids)))
            records = result.scalars().all()

            for record in records:
                upd = update_map.get(record.id)
                if not upd:
                    continue
                if "status" in upd:
                    record.status = upd["status"]
                if "analysis_result" in upd:
                    record.analysis_result = upd["analysis_result"]
                if "duration_seconds" in upd:
                    record.duration_seconds = upd["duration_seconds"]

            await session.flush()

            # 阶段 3 后置：对 completed 记录查 source 并发飞书通知
            for upd in updates:
                if not upd.get("_need_success_notify"):
                    continue
                try:
                    from models import WebhookEvent

                    # 查关联的 webhook_event_id
                    rec_dict = next((d for d in pending_dicts if d["id"] == upd["id"]), None)
                    if not rec_dict:
                        continue
                    evt_stmt = select(WebhookEvent).filter_by(id=rec_dict["webhook_event_id"])
                    evt_result = await session.execute(evt_stmt)
                    event = evt_result.scalars().first()
                    source = event.source if event else ""
                    notify_dict = {**rec_dict, **upd}
                    asyncio.create_task(_notify_feishu_deep_analysis(notify_dict, source))
                except Exception as e:
                    logger.debug(f"飞书深度分析通知失败: {e}")

    except Exception as e:
        logger.error(f"轮询任务异常: {e}")


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
