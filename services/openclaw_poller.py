"""OpenClaw 分析结果后台轮询"""
import asyncio
import json
import logging
import threading
import time
from datetime import datetime

import core.redis_client
from core.config import Config

logger = logging.getLogger('webhook_service.openclaw_poller')

# 轮询稳定性缓存：{analysis_id: {"msg_count": N, "text_len": M, "hit_count": int, "first_result": {...}}}
# 需要连续 N 次轮询结果一致才确认完成，避免过早提取中间结果
# 如果连续超时超过 MAX_CONSECUTIVE_ERRORS 次且已有首次结果，则降级使用首次结果
# 移除原有的内存锁和缓存字典


async def await _get_poll_stability(record_id: int) -> dict:
    redis_client = core.redis_client.get_redis()
    val = await redis_client.get(f"openclaw:poller:stability:{record_id}")
    return json.loads(val) if val else None

async def await _set_poll_stability(record_id: int, data: dict):
    redis_client = core.redis_client.get_redis()
    # 缓存保留 1 小时
    redis_client.setex(f"openclaw:poller:stability:{record_id}", 3600, json.dumps(data))

async def await _clear_poll_stability(record_id: int):
    redis_client = core.redis_client.get_redis()
    await redis_client.delete(f"openclaw:poller:stability:{record_id}")


def _notify_feishu_deep_analysis(record, source: str = ''):
    """发送深度分析完成的飞书通知"""
    from adapters.ecosystem_adapters import send_feishu_deep_analysis
    from core.config import Config

    webhook_url = Config.DEEP_ANALYSIS_FEISHU_WEBHOOK
    if not webhook_url:
        return

    try:
        analysis_data = {
            'analysis_result': record.analysis_result,
            'engine': record.engine,
            'duration_seconds': record.duration_seconds or 0,
        }
        asyncio.run(send_feishu_deep_analysis(
            webhook_url=webhook_url,
            analysis_record=analysis_data,
            source=source,
            webhook_event_id=record.webhook_event_id
        ))
    except Exception as e:
        logger.warning(f"飞书深度分析通知失败: {e}")


def _notify_feishu_deep_analysis_failed(record, reason: str = ''):
    """发送深度分析失败的飞书通知"""
    from adapters.ecosystem_adapters import send_feishu_deep_analysis
    from core.config import Config

    webhook_url = Config.DEEP_ANALYSIS_FEISHU_WEBHOOK
    if not webhook_url:
        return

    try:
        # 构建失败结果
        failed_result = record.analysis_result.copy() if record.analysis_result else {}
        failed_result['analysis_failed'] = True
        failed_result['failure_reason'] = reason

        analysis_data = {
            'analysis_result': failed_result,
            'engine': record.engine,
            'duration_seconds': record.duration_seconds or 0,
        }
        asyncio.run(send_feishu_deep_analysis(
            webhook_url=webhook_url,
            analysis_record=analysis_data,
            source='',
            webhook_event_id=record.webhook_event_id
        ))
        logger.info(f"深度分析失败通知已发送: id={record.id}, reason={reason}")
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
        _poll_pending_analyses_inner()
    except Exception as e:
        logger.error(f"[Poller] 执行内部轮询逻辑时发生错误: {e}", exc_info=True)
    finally:
        await redis_client.delete(lock_key)


def _poll_via_http(session_key: str, retry_count: int = 3) -> dict:
    """
    通过 HTTP API /final 接口获取分析结果（带重试）

    Returns:
        - 成功: {"status": "completed", "text": "...", "msg_count": N}
        - 暂无结果: {"status": "pending"}
        - 错误: {"status": "error", "error": "..."}
    """
    import requests as req

    base_url = Config.OPENCLAW_HTTP_API_URL.rstrip('/')
    last_error = None

    # 使用 hooks token 认证
    hooks_token = Config.OPENCLAW_HOOKS_TOKEN or Config.OPENCLAW_GATEWAY_TOKEN
    headers = {
        "Authorization": f"Bearer {hooks_token}"
    }

    for attempt in range(retry_count):
        try:
            # 使用 /final 接口直接获取最终结果
            url = f"{base_url}/sessions/{session_key}/final"
            logger.info(f"HTTP /final 请求 (尝试 {attempt + 1}/{retry_count}): {url}")

            response = req.get(url, headers=headers, timeout=30)

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
            is_final = data.get('isFinal', False)
            is_processing = data.get('isProcessing', False)
            text = data.get('text', '')
            msg_count = data.get('messageCount', 0)

            # 判断是否完成
            if is_processing and not text:
                last_error = "分析进行中"
                continue

            if text:
                return {
                    "status": "completed",
                    "text": text,
                    "msg_count": msg_count
                }

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


def _poll_pending_analyses_inner():
    """轮询逻辑主体"""
    from core.config import Config
    from db.session import session_scope
    from models import DeepAnalysis
    from services.openclaw_ws_client import poll_session_result

    try:
        with session_scope() as session:
            pending = session.query(DeepAnalysis).filter_by(
                status='pending'
            ).order_by(DeepAnalysis.created_at.asc()).limit(10).all()

            if not pending:
                return

            logger.info(f"[Poller] 扫描到待处理分析: count={len(pending)}")

            for record in pending:
                try:
                    timeout_seconds = getattr(Config, 'OPENCLAW_TIMEOUT_SECONDS', 300)
                    if record.created_at and (datetime.now() - record.created_at).total_seconds() > timeout_seconds:
                        record.status = 'failed'
                        record.analysis_result = {'root_cause': 'OpenClaw 分析超时'}
                        await _clear_poll_stability(record.id)
                        _notify_feishu_deep_analysis_failed(record, '超时失败')
                        continue

                    if not record.openclaw_session_key:
                        record.status = 'failed'
                        await _clear_poll_stability(record.id)
                        continue

                    # 最小等待时间
                    elapsed = (datetime.now() - record.created_at).total_seconds() if record.created_at else 999
                    if elapsed < Config.OPENCLAW_MIN_WAIT_SECONDS:
                        continue

                    if Config.OPENCLAW_HTTP_API_URL:
                        result = _poll_via_http(record.openclaw_session_key)
                    else:
                        result = poll_session_result(
                            gateway_url=Config.OPENCLAW_GATEWAY_URL,
                            gateway_token=Config.OPENCLAW_GATEWAY_TOKEN,
                            session_key=record.openclaw_session_key,
                            timeout=Config.OPENCLAW_POLL_TIMEOUT
                        )

                    if result.get('status') == 'completed':
                        text = result.get('text', '')
                        msg_count = result.get('msg_count', 0)

                        current_snapshot = {'msg_count': msg_count, 'text_len': len(text)}
                        prev_snapshot = await _get_poll_stability(record.id)

                        if prev_snapshot and prev_snapshot['msg_count'] == current_snapshot['msg_count'] and prev_snapshot['text_len'] == current_snapshot['text_len']:
                            hit_count = prev_snapshot.get('hit_count', 1) + 1
                            if hit_count >= Config.OPENCLAW_STABILITY_REQUIRED_HITS:
                                logger.debug(f"[Poller] 分析稳定确认: id={record.id}")
                            else:
                                await _set_poll_stability(record.id, {**current_snapshot, 'hit_count': hit_count})
                                continue

                            await _clear_poll_stability(record.id)
                            parsed_result = None
                            json_text = _extract_robust_json(text)
                            if json_text:
                                try:
                                    parsed_result = json.loads(json_text)
                                except Exception:
                                    parsed_result = None

                            if parsed_result and isinstance(parsed_result, dict):
                                parsed_result['_openclaw_run_id'] = record.openclaw_run_id
                                parsed_result['_openclaw_text'] = text
                                record.analysis_result = parsed_result
                            else:
                                record.analysis_result = {
                                    'root_cause': text,
                                    '_openclaw_text': text
                                }

                            record.status = 'completed'
                            record.duration_seconds = (datetime.now() - record.created_at).total_seconds() if record.created_at else 0

                            try:
                                from models import WebhookEvent
                                event = session.query(WebhookEvent).filter_by(id=record.webhook_event_id).first()
                                source = event.source if event else ''
                                _notify_feishu_deep_analysis(record, source)
                            except Exception as e:
                                logger.debug(f"飞书深度分析通知失败: {e}")
                        else:
                            await _set_poll_stability(record.id, {**current_snapshot, 'hit_count': 1, 'first_result': {'text': text}})

                    elif result.get('status') == 'error':
                        prev_snapshot = await _get_poll_stability(record.id)
                        if prev_snapshot and 'first_result' in prev_snapshot:
                            error_count = prev_snapshot.get('error_count', 0) + 1
                            if error_count >= Config.OPENCLAW_MAX_CONSECUTIVE_ERRORS and Config.OPENCLAW_ENABLE_DEGRADATION:
                                text = prev_snapshot['first_result']['text']
                                await _clear_poll_stability(record.id)
                                parsed_result = None
                                json_text = _extract_robust_json(text)
                                if json_text:
                                    try:
                                        parsed_result = json.loads(json_text)
                                    except Exception:
                                        parsed_result = None

                                record.analysis_result = parsed_result or {'root_cause': text}
                                record.status = 'completed'
                                continue
                        await _clear_poll_stability(record.id)

                except Exception as e:
                    logger.error(f"轮询记录 id={record.id} 失败: {e}")

    except Exception as e:
        logger.error(f"轮询任务异常: {e}")


from services.pollers import _stop_event
def start_poller(interval: int = 30):
    """启动后台轮询线程"""
    def _loop():
        logger.info(f"[Poller] 任务已启动: interval={interval}s")
        import asyncio
        while not _stop_event.is_set():
            try:
                asyncio.run(poll_pending_analyses())
            except Exception as e:
                logger.error(f"轮询循环异常: {e}")
            _stop_event.wait(interval)

    t = threading.Thread(target=_loop, daemon=True, name='openclaw-poller')
    t.start()
    return t

def _extract_robust_json(text: str) -> str | None:
    """从文本中寻找并提取第一个完整的 JSON 对象（处理嵌套大括号）"""
    try:
        start_idx = text.find('{')
        if start_idx == -1:
            return None
        stack = 0
        for i in range(start_idx, len(text)):
            if text[i] == '{':
                stack += 1
            elif text[i] == '}':
                stack -= 1
                if stack == 0:
                    return text[start_idx:i+1]
    except Exception:
        return None
    return None
