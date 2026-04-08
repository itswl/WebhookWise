"""OpenClaw 分析结果后台轮询"""
import time
import json
import re
import threading
import logging
from datetime import datetime, timedelta

from core.config import Config

logger = logging.getLogger('webhook_service.openclaw_poller')

# 轮询稳定性缓存：{analysis_id: {"msg_count": N, "text_len": M, "hit_count": int, "first_result": {...}}}
# 需要连续 N 次轮询结果一致才确认完成，避免过早提取中间结果
# 如果连续超时超过 MAX_CONSECUTIVE_ERRORS 次且已有首次结果，则降级使用首次结果
_poll_stability_cache = {}
_poll_lock = threading.Lock()


def _notify_feishu_deep_analysis(record, source: str = ''):
    """发送深度分析完成的飞书通知"""
    from core.config import Config
    from adapters.ecosystem_adapters import send_feishu_deep_analysis
    
    webhook_url = Config.DEEP_ANALYSIS_FEISHU_WEBHOOK
    if not webhook_url:
        return
    
    try:
        analysis_data = {
            'analysis_result': record.analysis_result,
            'engine': record.engine,
            'duration_seconds': record.duration_seconds or 0,
        }
        send_feishu_deep_analysis(
            webhook_url=webhook_url,
            analysis_record=analysis_data,
            source=source,
            webhook_event_id=record.webhook_event_id
        )
    except Exception as e:
        logger.warning(f"飞书深度分析通知失败: {e}")


def _notify_feishu_deep_analysis_failed(record, reason: str = ''):
    """发送深度分析失败的飞书通知"""
    from core.config import Config
    from adapters.ecosystem_adapters import send_feishu_deep_analysis
    
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
        send_feishu_deep_analysis(
            webhook_url=webhook_url,
            analysis_record=analysis_data,
            source='',
            webhook_event_id=record.webhook_event_id
        )
        logger.info(f"深度分析失败通知已发送: id={record.id}, reason={reason}")
    except Exception as e:
        logger.warning(f"飞书深度分析失败通知失败: {e}")

def poll_pending_analyses():
    """查询所有 status='pending' 的 DeepAnalysis 记录，逐一轮询结果"""
    # 防止多个轮询器并发执行（Docker 多 worker 场景）
    if not _poll_lock.acquire(blocking=False):
        logger.debug("另一个轮询正在执行，跳过本轮")
        return
    try:
        _poll_pending_analyses_inner()
    finally:
        _poll_lock.release()




def _poll_via_http(session_key: str, limit: int = 100) -> dict:
    """
    通过 HTTP API 轮询获取分析结果
    
    Returns:
        - 成功: {"status": "completed", "text": "...", "msg_count": N}
        - 暂无结果: {"status": "pending"}
        - 错误: {"status": "error", "error": "..."}
    """
    base_url = Config.OPENCLAW_HTTP_API_URL.rstrip('/')
    
    try:
        import requests as req
        url = f"{base_url}/sessions/{session_key}/messages?limit={limit}"
        logger.debug(f"HTTP 轮询请求: {url}")
        
        response = req.get(url, timeout=30)
        
        if response.status_code == 404:
            return {"status": "error", "error": "Session not found"}
        
        if response.status_code != 200:
            return {"status": "error", "error": f"HTTP {response.status_code}"}
        
        data = response.json()
        messages = data.get('messages', [])
        
        if not messages:
            return {"status": "pending"}
        
        # 提取最后一条 assistant 消息的文本
        final_text = None
        for msg in reversed(messages):
            if msg.get('role') == 'assistant':
                content_list = msg.get('content', [])
                if isinstance(content_list, list):
                    for c in content_list:
                        if isinstance(c, dict) and c.get('type') == 'text':
                            final_text = c.get('content', '')
                            break
                elif isinstance(content_list, str):
                    final_text = content_list
                if final_text:
                    break
        
        if not final_text:
            return {"status": "pending"}
        
        return {
            "status": "completed",
            "text": final_text,
            "message": msg,
            "msg_count": len(messages)
        }
        
    except Exception as e:
        logger.warning(f"HTTP 轮询失败: {e}")
        return {"status": "error", "error": str(e)}

def _poll_pending_analyses_inner():
    """轮询逻辑主体（由 poll_pending_analyses 在持锁状态下调用）"""
    from core.models import DeepAnalysis, session_scope
    from core.config import Config
    from services.openclaw_ws_client import poll_session_result

    try:
        with session_scope() as session:
            # 查询 pending 记录（限制批次大小）
            pending = session.query(DeepAnalysis).filter_by(
                status='pending'
            ).order_by(DeepAnalysis.created_at.asc()).limit(10).all()

            if not pending:
                return

            logger.info(f"发现 {len(pending)} 条待轮询的 OpenClaw 分析")

            for record in pending:
                try:
                    # 检查是否超时（created_at 距今超过 OPENCLAW_TIMEOUT_SECONDS）
                    timeout_seconds = getattr(Config, 'OPENCLAW_TIMEOUT_SECONDS', 300)
                    if record.created_at and (datetime.now() - record.created_at).total_seconds() > timeout_seconds:
                        record.status = 'failed'
                        record.analysis_result = {
                            'root_cause': 'OpenClaw 分析超时',
                            'impact': '分析未在规定时间内完成',
                            'recommendations': ['请重新触发分析', '检查 OpenClaw 服务状态'],
                            'confidence': 0
                        }
                        # 清理稳定性缓存
                        _poll_stability_cache.pop(record.id, None)
                        logger.warning(f"分析超时: id={record.id}, run_id={record.openclaw_run_id}")
                        # 发送失败通知
                        _notify_feishu_deep_analysis_failed(record, '超时失败')
                        continue

                    # 没有 session_key 的记录无法轮询
                    if not record.openclaw_session_key:
                        record.status = 'failed'
                        record.analysis_result = {'root_cause': '缺少 sessionKey，无法轮询'}
                        # 清理稳定性缓存
                        _poll_stability_cache.pop(record.id, None)
                        # 发送失败通知
                        _notify_feishu_deep_analysis_failed(record, '缺少sessionKey')
                        continue

                    # 最小等待时间检查
                    elapsed = (datetime.now() - record.created_at).total_seconds() if record.created_at else 999
                    if elapsed < Config.OPENCLAW_MIN_WAIT_SECONDS:
                        logger.debug(f"分析创建未满 {Config.OPENCLAW_MIN_WAIT_SECONDS}s (已 {elapsed:.0f}s), 跳过: id={record.id}")
                        continue

                    # 根据配置选择获取方式
                    if Config.OPENCLAW_HTTP_API_URL:
                        # 使用 HTTP API 轮询
                        result = _poll_via_http(record.openclaw_session_key)
                        logger.debug(f"HTTP 轮询结果: id={record.id}, status={result.get('status')}")
                    else:
                        # 使用 WebSocket 轮询
                        result = poll_session_result(
                            gateway_url=Config.OPENCLAW_GATEWAY_URL,
                            gateway_token=Config.OPENCLAW_GATEWAY_TOKEN,
                            session_key=record.openclaw_session_key,
                            timeout=Config.OPENCLAW_POLL_TIMEOUT
                        )

                    if result.get('status') == 'completed':
                        text = result.get('text', '')
                        message = result.get('message', {})
                        msg_count = result.get('msg_count', 0)

                        # 稳定性检测：连续 N 次轮询结果一致才确认完成
                        current_snapshot = {
                            'msg_count': msg_count,
                            'text_len': len(text)
                        }
                        prev_snapshot = _poll_stability_cache.get(record.id)

                        if prev_snapshot and prev_snapshot['msg_count'] == current_snapshot['msg_count'] and prev_snapshot['text_len'] == current_snapshot['text_len']:
                            # 结果与上次一致，增加命中计数
                            hit_count = prev_snapshot.get('hit_count', 1) + 1

                            if hit_count >= Config.OPENCLAW_STABILITY_REQUIRED_HITS:
                                # 达到要求的连续一致次数 → 真正完成
                                logger.info(f"分析稳定确认: id={record.id}, hits={hit_count}, msg_count={current_snapshot['msg_count']}, text_len={current_snapshot['text_len']}")
                            else:
                                # 还未达到，继续等待
                                _poll_stability_cache[record.id] = {**current_snapshot, 'hit_count': hit_count}
                                logger.info(f"分析结果一致({hit_count}/{Config.OPENCLAW_STABILITY_REQUIRED_HITS}), 继续等待: id={record.id}, msg_count={current_snapshot['msg_count']}, text_len={current_snapshot['text_len']}")
                                continue

                            # 清理缓存
                            _poll_stability_cache.pop(record.id, None)

                            # 尝试从文本中提取 JSON 结构化数据
                            parsed_result = None
                            json_match = re.search(r'\{[\s\S]*\}', text)
                            if json_match:
                                try:
                                    parsed_result = json.loads(json_match.group())
                                except json.JSONDecodeError:
                                    pass

                            if parsed_result and isinstance(parsed_result, dict):
                                parsed_result['_openclaw_run_id'] = record.openclaw_run_id
                                parsed_result['_openclaw_text'] = text
                                record.analysis_result = parsed_result
                            else:
                                record.analysis_result = {
                                    'root_cause': text,
                                    'impact': '',
                                    'recommendations': [],
                                    'confidence': 0.5,
                                    '_openclaw_run_id': record.openclaw_run_id,
                                    '_openclaw_text': text
                                }

                            record.status = 'completed'
                            record.duration_seconds = (datetime.now() - record.created_at).total_seconds() if record.created_at else 0
                            logger.info(f"分析完成: id={record.id}, run_id={record.openclaw_run_id}, text_len={len(text)}")

                            # 获取告警来源并发送飞书通知
                            try:
                                from core.models import WebhookEvent
                                event = session.query(WebhookEvent).filter_by(id=record.webhook_event_id).first()
                                source = event.source if event else ''
                                _notify_feishu_deep_analysis(record, source)
                            except Exception as notify_err:
                                logger.warning(f"发送飞书通知失败: {notify_err}")
                        else:
                            # 首次获取或结果有变化 → 重置计数
                            change_type = '变化' if prev_snapshot else '首次获取'
                            logger.info(f"分析结果{change_type}, 开始稳定计数: id={record.id}, msg_count={current_snapshot['msg_count']}, text_len={current_snapshot['text_len']}")
                            # 首次获取时更新 created_at，延长超时窗口（因为分析已完成，只需等待稳定性确认）
                            if not prev_snapshot:
                                record.created_at = datetime.now()
                                logger.debug(f"首次获取结果，更新 created_at 以延长超时窗口: id={record.id}")
                            # 保存首次结果用于降级
                            _poll_stability_cache[record.id] = {
                                **current_snapshot,
                                'hit_count': 1,
                                'first_result': {'text': text, 'message': message}
                            }
                            # 不保存，继续下一轮轮询

                    elif result.get('status') == 'pending':
                        # 仍在分析中，清理缓存（重新开始计数）
                        _poll_stability_cache.pop(record.id, None)
                        logger.debug(f"分析进行中: id={record.id}, run_id={record.openclaw_run_id}")

                    elif result.get('status') == 'error':
                        error = result.get('error', 'unknown')
                        logger.warning(f"轮询出错: id={record.id}, error={error}")
                        
                        # 检查是否有首次结果可以降级使用
                        prev_snapshot = _poll_stability_cache.get(record.id)
                        if prev_snapshot and 'first_result' in prev_snapshot:
                            error_count = prev_snapshot.get('error_count', 0) + 1
                            if error_count >= Config.OPENCLAW_MAX_CONSECUTIVE_ERRORS:
                                # 连续超时过多，降级使用首次结果
                                logger.warning(f"连续超时 {error_count} 次，降级使用首次结果: id={record.id}")
                                first_result = prev_snapshot['first_result']
                                text = first_result['text']
                                message = first_result['message']
                                _poll_stability_cache.pop(record.id, None)
                                
                                # 尝试从文本中提取 JSON 结构化数据
                                parsed_result = None
                                json_match = re.search(r'\{[\s\S]*\}', text)
                                if json_match:
                                    try:
                                        parsed_result = json.loads(json_match.group())
                                    except json.JSONDecodeError:
                                        pass
                                
                                if parsed_result and isinstance(parsed_result, dict):
                                    parsed_result['_openclaw_run_id'] = record.openclaw_run_id
                                    parsed_result['_openclaw_text'] = text
                                    record.analysis_result = parsed_result
                                else:
                                    record.analysis_result = {
                                        'root_cause': text,
                                        'impact': '',
                                        'recommendations': [],
                                        'confidence': 0.5,
                                        '_openclaw_run_id': record.openclaw_run_id,
                                        '_openclaw_text': text
                                    }
                                
                                record.status = 'completed'
                                record.duration_seconds = (datetime.now() - record.created_at).total_seconds() if record.created_at else 0
                                logger.info(f"分析完成(降级): id={record.id}, run_id={record.openclaw_run_id}, text_len={len(text)}")
                                
                                # 获取告警来源并发送飞书通知
                                try:
                                    from core.models import WebhookEvent
                                    event = session.query(WebhookEvent).filter_by(id=record.webhook_event_id).first()
                                    source = event.source if event else ''
                                    _notify_feishu_deep_analysis(record, source)
                                except Exception as notify_err:
                                    logger.warning(f"发送飞书通知失败: {notify_err}")
                                continue
                            else:
                                # 增加错误计数
                                prev_snapshot['error_count'] = error_count
                                logger.debug(f"降级倒计时: {error_count}/{Config.OPENCLAW_MAX_CONSECUTIVE_ERRORS}: id={record.id}")
                        else:
                            # 清理稳定性缓存
                            _poll_stability_cache.pop(record.id, None)
                        # 不立即标记失败，下次重试

                except Exception as e:
                    logger.error(f"轮询记录 id={record.id} 失败: {e}")

    except Exception as e:
        logger.error(f"轮询任务异常: {e}")


def start_poller(interval: int = 30):
    """启动后台轮询线程"""
    def _loop():
        logger.info(f"OpenClaw 轮询任务已启动，间隔 {interval} 秒")
        while True:
            try:
                poll_pending_analyses()
            except Exception as e:
                logger.error(f"轮询循环异常: {e}")
            time.sleep(interval)
    
    t = threading.Thread(target=_loop, daemon=True, name='openclaw-poller')
    t.start()
    return t
