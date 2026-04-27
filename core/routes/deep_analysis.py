"""
core/routes/deep_analysis.py
============================
深度分析相关路由：触发分析、列表查询、转发、重试。
"""
import contextlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

from core.config import Config
from core.http_client import get_http_client
from core.logger import logger
from core.models import DeepAnalysis, WebhookEvent, session_scope
from services.ai_analyzer import analyze_with_openclaw

deep_analysis_router = APIRouter()


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def _determine_engine(requested_engine: str) -> str:
    """根据请求和配置决定使用哪个引擎"""
    if requested_engine == 'local':
        return 'local'
    elif requested_engine == 'openclaw':
        if Config.OPENCLAW_ENABLED:
            return 'openclaw'
        logger.warning("OpenClaw 未启用，回退到本地 AI")
        return 'local'
    else:  # auto
        if Config.DEEP_ANALYSIS_ENGINE == 'openclaw' and Config.OPENCLAW_ENABLED:
            return 'openclaw'
        elif Config.DEEP_ANALYSIS_ENGINE == 'local':
            return 'local'
        return 'openclaw' if Config.OPENCLAW_ENABLED else 'local'


async def _local_ai_analysis(alert_data: dict, user_question: str) -> tuple[dict, float]:
    """本地 AI 深度分析"""
    start_time = time.time()

    if not Config.OPENAI_API_KEY:
        raise ValueError('AI 服务未配置')

    prompt_path = Path(__file__).parent.parent.parent / 'prompts' / 'deep_analysis.txt'
    try:
        with open(prompt_path, encoding='utf-8') as f:
            system_prompt = f.read()
    except FileNotFoundError:
        system_prompt = """你是一个专业的 SRE 分析专家。请对以下告警进行深度分析，包括：
1. 根因分析
2. 影响范围评估
3. 修复建议
请用 JSON 格式返回分析结果。"""

    alert_json = json.dumps(alert_data, ensure_ascii=False, indent=2)
    user_prompt = f"""请对以下告警进行深度分析：

## 告警数据
```json
{alert_json}
```
"""
    if user_question:
        user_prompt += f"\n## 用户问题\n{user_question}\n"

    user_prompt += """
请返回 JSON 格式的分析报告，包含以下字段：
- root_cause: 根因分析
- impact: 影响范围
- recommendations: 修复建议列表
- confidence: 置信度 (0-1)
"""

    client = AsyncOpenAI(api_key=Config.OPENAI_API_KEY, base_url=Config.OPENAI_API_URL)
    response = await client.chat.completions.create(
        model=Config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=Config.OPENAI_TEMPERATURE,
        max_tokens=Config.OPENAI_MAX_TOKENS * 2
    )

    ai_response = response.choices[0].message.content.strip()
    duration = time.time() - start_time

    try:
        json_match = re.search(r'```json\s*([\s\S]*?)```', ai_response)
        report = json.loads(json_match.group(1)) if json_match else json.loads(ai_response)
    except (json.JSONDecodeError, TypeError):
        report = {
            'root_cause': ai_response,
            'impact': '请查看上方分析',
            'recommendations': [],
            'confidence': 0.5
        }

    return report, duration


# ── 路由 ─────────────────────────────────────────────────────────────────────

@deep_analysis_router.post('/api/deep-analyze/{webhook_id}')
async def deep_analyze_webhook(webhook_id: int, payload: dict = None):
    """
    触发深度分析（支持多引擎）

    请求体（可选）:
    {
        "user_question": "用户问题",
        "engine": "auto"  // local | openclaw | auto
    }
    """
    payload = payload or {}
    try:
        with session_scope() as session:
            event = session.query(WebhookEvent).filter_by(id=webhook_id).first()
            if not event:
                return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

            alert_data = event.parsed_data or {}
            if not alert_data and event.raw_payload:
                try:
                    alert_data = json.loads(event.raw_payload)
                except (json.JSONDecodeError, TypeError):
                    alert_data = {"raw": event.raw_payload}

            user_question = payload.get('user_question', '')
            requested_engine = payload.get('engine', 'auto')

            engine = _determine_engine(requested_engine)
            logger.info(f"开始深度分析 webhook ID: {webhook_id}, engine: {engine}")

            if engine == 'openclaw':
                start_time = time.time()
                webhook_data = {'source': event.source, 'parsed_data': alert_data}
                result = await analyze_with_openclaw(webhook_data, user_question)
                duration = time.time() - start_time

                if result.get('_degraded'):
                    logger.warning(f"OpenClaw 分析失败，降级到本地 AI: {result.get('_degraded_reason')}")
                    try:
                        report, duration = await _local_ai_analysis(alert_data, user_question)
                        engine = 'local (fallback)'
                    except Exception as e:
                        return JSONResponse(status_code=500, content={"success": False, "error": f"深度分析失败: {str(e)}"})
                elif result.get('_pending'):
                    run_id = result.get('_openclaw_run_id', '')
                    session_key = result.get('_openclaw_session_key', '')
                    report = {
                        'status': 'pending',
                        'root_cause': 'OpenClaw Agent 正在分析中，结果将自动更新...',
                        'impact': '分析已触发，预计几分钟内完成',
                        'recommendations': ['请稍后刷新页面查看分析结果'],
                        'confidence': 0
                    }
                    logger.info(f"OpenClaw 分析已触发: run_id={run_id}, session_key={session_key}")
                else:
                    report = result
            else:
                report, duration = await _local_ai_analysis(alert_data, user_question)
                result = {}  # Initialize result to prevent UnboundLocalError

            record_id = None
            try:
                deep_record = DeepAnalysis(
                    webhook_event_id=webhook_id,
                    engine=engine,
                    user_question=user_question or '',
                    analysis_result=report,
                    duration_seconds=duration,
                    openclaw_run_id=result.get('_openclaw_run_id', '') if isinstance(result, dict) else '',
                    openclaw_session_key=result.get('_openclaw_session_key', '') if isinstance(result, dict) else '',
                    status='pending' if isinstance(result, dict) and result.get('_pending') else 'completed'
                )
                session.add(deep_record)
                session.flush()
                record_id = deep_record.id
                logger.info(f"深度分析结果已保存: id={record_id}, webhook_id={webhook_id}")

                if deep_record.status == 'completed':
                    try:
                        from adapters.ecosystem_adapters import send_feishu_deep_analysis
                        if Config.DEEP_ANALYSIS_FEISHU_WEBHOOK:
                            await send_feishu_deep_analysis(
                                webhook_url=Config.DEEP_ANALYSIS_FEISHU_WEBHOOK,
                                analysis_record={
                                    'analysis_result': report,
                                    'engine': engine,
                                    'duration_seconds': duration,
                                },
                                source=event.source,
                                webhook_event_id=webhook_id
                            )
                    except Exception as notify_err:
                        logger.warning(f"飞书深度分析通知失败: {notify_err}")
            except Exception as e:
                logger.error(f"保存深度分析结果失败: {e}")

            return {
                "success": True,
                "data": {
                    'analysis': report,
                    'duration_seconds': round(duration, 2),
                    'engine': engine,
                    'record_id': record_id
                }
            }

    except Exception as e:
        logger.error(f"深度分析失败: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@deep_analysis_router.get('/api/deep-analyses')
def list_all_deep_analyses(
    page: int = Query(1),
    per_page: int = Query(20),
    cursor: int | None = Query(None),
    status_filter: str = Query('', alias='status'),
    engine_filter: str = Query('', alias='engine')
):
    """获取所有深度分析记录（分页 + 筛选）"""
    per_page = max(1, min(per_page, 100))

    with session_scope() as session:
        # 总数（始终计算）
        count_query = session.query(DeepAnalysis)
        if status_filter:
            count_query = count_query.filter(DeepAnalysis.status == status_filter)
        if engine_filter:
            count_query = count_query.filter(DeepAnalysis.engine == engine_filter)
        total = count_query.count()

        # 主查询
        query = session.query(DeepAnalysis).order_by(DeepAnalysis.id.desc())

        if status_filter:
            query = query.filter(DeepAnalysis.status == status_filter)
        if engine_filter:
            query = query.filter(DeepAnalysis.engine == engine_filter)

        if cursor is not None:
            query = query.filter(DeepAnalysis.id < cursor)
        else:
            offset = (page - 1) * per_page
            query = query.offset(offset)

        records = query.limit(per_page).all()

        webhook_ids = [r.webhook_event_id for r in records]
        webhook_map = {}
        if webhook_ids:
            events = session.query(
                WebhookEvent.id, WebhookEvent.source,
                WebhookEvent.is_duplicate, WebhookEvent.beyond_window
            ).filter(
                WebhookEvent.id.in_(webhook_ids)
            ).all()
            webhook_map = {e.id: e for e in events}

        data = []
        for r in records:
            d = r.to_dict()
            webhook = webhook_map.get(r.webhook_event_id)
            d['source'] = webhook.source if webhook else 'unknown'
            d['is_duplicate'] = bool(webhook and webhook.is_duplicate) if webhook else False
            d['beyond_window'] = bool(webhook and webhook.beyond_window) if webhook else False
            data.append(d)

        next_cursor = records[-1].id if len(records) == per_page else None
        return {
            "success": True,
            "data": {
                'items': data,
                'total': total,
                'next_cursor': next_cursor,
                'page': page,
                'per_page': per_page,
                'total_pages': (total + per_page - 1) // per_page if total > 0 else 0,
            }
        }


@deep_analysis_router.get('/api/deep-analyses/{webhook_id}')
def get_deep_analyses(webhook_id: int):
    """获取某告警的所有深度分析记录"""
    with session_scope() as session:
        records = session.query(DeepAnalysis).filter_by(
            webhook_event_id=webhook_id
        ).order_by(DeepAnalysis.created_at.desc()).all()
        return {
            "success": True,
            "data": [r.to_dict() for r in records]
        }


@deep_analysis_router.post('/api/deep-analyses/{analysis_id}/forward')
async def forward_deep_analysis(analysis_id: int, payload: dict = None):
    """转发深度分析结果到指定目标"""
    payload = payload or {}
    try:
        target_url = (payload.get('target_url') or '').strip()
        if not target_url:
            return JSONResponse(status_code=400, content={'success': False, 'message': '转发 URL 不能为空'})
        if not target_url.startswith(('http://', 'https://')):
            return JSONResponse(status_code=400, content={'success': False, 'message': 'URL 格式无效'})

        with session_scope() as session:
            analysis = session.query(DeepAnalysis).get(analysis_id)
            if not analysis:
                return JSONResponse(status_code=404, content={'success': False, 'message': '分析记录不存在'})
            if analysis.status != 'completed':
                return JSONResponse(status_code=400, content={'success': False, 'message': '分析尚未完成'})

            source = 'unknown'
            if analysis.webhook_event_id:
                event = session.query(WebhookEvent).get(analysis.webhook_event_id)
                if event:
                    source = event.source or 'unknown'

            is_feishu = 'feishu.cn' in target_url or 'larksuite.com' in target_url

            if is_feishu:
                from adapters.ecosystem_adapters import send_feishu_deep_analysis
                success = await send_feishu_deep_analysis(
                    webhook_url=target_url,
                    analysis_record={
                        'analysis_result': analysis.analysis_result,
                        'engine': analysis.engine,
                        'duration_seconds': analysis.duration_seconds
                    },
                    source=source,
                    webhook_event_id=analysis.webhook_event_id or 0
                )
                if success:
                    return {'success': True, 'message': '已发送到飞书'}
                return JSONResponse(status_code=500, content={'success': False, 'message': '飞书发送失败'})
            else:
                fwd_payload = {
                    'type': 'deep_analysis',
                    'analysis_id': analysis_id,
                    'source': source,
                    'engine': analysis.engine,
                    'webhook_event_id': analysis.webhook_event_id,
                    'analysis_result': analysis.analysis_result,
                    'duration_seconds': analysis.duration_seconds,
                    'created_at': analysis.created_at.isoformat() if analysis.created_at else None
                }
                client = get_http_client()
                resp = await client.post(target_url, json=fwd_payload, timeout=Config.FORWARD_TIMEOUT)
                resp.raise_for_status()
                return {'success': True, 'message': f'已转发 (HTTP {resp.status_code})'}
    except Exception as e:
        logger.error(f"转发深度分析失败: {e}")
        return JSONResponse(status_code=500, content={'success': False, 'message': str(e)})


@deep_analysis_router.post('/api/deep-analyses/{analysis_id}/retry')
async def retry_deep_analysis(analysis_id: int):
    """
    重新拉取深度分析结果
    
    策略：
    - 配置了 OPENCLAW_HTTP_API_URL → 直接通过 HTTP API 获取
    - 未配置 → 重置为 pending，由轮询器通过 WebSocket 获取
    """
    try:
        with session_scope() as session:
            record = session.query(DeepAnalysis).get(analysis_id)
            if not record:
                return JSONResponse(status_code=404, content={'error': '分析记录不存在'})

            if record.status not in ('failed', 'completed'):
                return JSONResponse(status_code=400, content={'error': f'只能在失败或已完成状态下重新拉取，当前状态: {record.status}'})

            if not record.openclaw_session_key:
                return JSONResponse(status_code=400, content={'error': '缺少 session key，无法重新拉取'})

            # 检查是否配置了 HTTP API URL
            if Config.OPENCLAW_HTTP_API_URL:
                # 直接通过 HTTP API 获取（复用轮询器的重试逻辑）
                from fastapi.concurrency import run_in_threadpool

                from services.openclaw_poller import _poll_via_http
                logger.info(f"通过 HTTP API 重新获取分析结果: id={analysis_id}")
                result = await run_in_threadpool(_poll_via_http, record.openclaw_session_key, retry_count=3)
                
                if result.get('status') == 'error':
                    return JSONResponse(status_code=400, content={'error': result.get('error', '获取失败')})
                
                if result.get('status') != 'completed':
                    return JSONResponse(status_code=400, content={'error': f'获取未完成: {result.get("status")}'})
                
                text = result.get('text', '')
                
                # 尝试解析 JSON
                parsed_result = None
                json_match = re.search(r'\{[\s\S]*\}', text)
                if json_match:
                    with contextlib.suppress(json.JSONDecodeError):
                        parsed_result = json.loads(json_match.group())
                
                if parsed_result and isinstance(parsed_result, dict):
                    parsed_result['_openclaw_run_id'] = record.openclaw_run_id
                    parsed_result['_openclaw_text'] = text
                    parsed_result['_fetched_via'] = 'http-retry'
                    record.analysis_result = parsed_result
                else:
                    record.analysis_result = {
                        'root_cause': text,
                        'impact': '',
                        'recommendations': [],
                        'confidence': 0.5,
                        '_openclaw_run_id': record.openclaw_run_id,
                        '_openclaw_text': text,
                        '_fetched_via': 'http-retry'
                    }
                
                record.status = 'completed'
                record.duration_seconds = (datetime.now() - record.created_at).total_seconds() if record.created_at else 0
                session.commit()
                
                logger.info(f"HTTP API 获取成功: id={analysis_id}, text_len={len(text)}")
                
                # 发送飞书通知
                try:
                    from adapters.ecosystem_adapters import send_feishu_deep_analysis
                    if Config.DEEP_ANALYSIS_FEISHU_WEBHOOK:
                        event = session.query(WebhookEvent).filter_by(id=record.webhook_event_id).first()
                        source = event.source if event else ''
                        analysis_data = {
                            'analysis_result': record.analysis_result,
                            'engine': record.engine,
                            'duration_seconds': record.duration_seconds,
                        }
                        await send_feishu_deep_analysis(
                            webhook_url=Config.DEEP_ANALYSIS_FEISHU_WEBHOOK,
                            analysis_record=analysis_data,
                            source=source,
                            webhook_event_id=record.webhook_event_id
                        )
                except Exception as notify_err:
                    logger.warning(f"飞书深度分析通知失败: {notify_err}")
                
                return {
                    'success': True, 
                    'message': f'获取成功！通过 HTTP API 获取了 {len(text)} 字符的分析结果'
                }
            else:
                # 没有配置 HTTP API，重置为 pending 让轮询器处理
                record.status = 'pending'
                record.created_at = datetime.now()
                record.analysis_result = None
                session.commit()

                logger.info(f"深度分析 #{analysis_id} 已重置为 pending，等待轮询重新拉取")
                return {'success': True, 'message': '已重新开始拉取，请等待结果'}
                
    except Exception as e:
        logger.error(f"重试深度分析失败: {e}")
        return JSONResponse(status_code=500, content={'error': str(e)})
