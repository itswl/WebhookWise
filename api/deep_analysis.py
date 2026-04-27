"""深度分析相关路由：触发分析、列表查询、转发、重试。"""
import contextlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from sqlalchemy import func, select

from core.config import Config
from core.http_client import get_http_client
from core.logger import logger
from db.session import session_scope
from models import DeepAnalysis, WebhookEvent

deep_analysis_router = APIRouter()


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def _determine_engine(requested_engine: str) -> str:
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
    start_time = time.time()

    if not Config.OPENAI_API_KEY:
        raise ValueError('AI 服务未配置')

    prompt_path = Path(__file__).parent.parent.parent / 'prompts' / 'deep_analysis.txt'
    try:
        with open(prompt_path, encoding='utf-8') as f:
            system_prompt = f.read()
    except FileNotFoundError:
        # 1. 根因分析
        # 2. 影响范围评估
        # 3. 修复建议

        pass

        # ## 告警数据
        # ```json
        # {alert_json}
        # ```
    if user_question:
        pass

        # 请返回 JSON 格式的分析报告
        # - root_cause:
        # - impact:
        # - recommendations:
        # - confidence:

    client = AsyncOpenAI(api_key=Config.OPENAI_API_KEY, base_url=Config.OPENAI_API_URL)
    response = await client.chat.completions.create(
        model=Config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_question}
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
    """触发深度分析（支持多引擎）"""
    payload = payload or {}
    try:
        async with session_scope() as session:
            result = await session.execute(select(WebhookEvent).filter_by(id=webhook_id))
            event = result.scalars().first()
            if not event:
                return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

            alert_data = event.parsed_data or {}
            if not alert_data and event.raw_payload:
                try:
                    alert_data = json.loads(event.raw_payload)
                except (json.JSONDecodeError, TypeError):
                    alert_data = {"raw": event.raw_payload}

            user_question = payload.get('user_question', '')
            engine_pref = payload.get('engine', 'auto')

            # ===== 多引擎路由逻辑 =====
            if engine_pref == 'openclaw' or (engine_pref == 'auto' and Config.OPENCLAW_ENABLED):
                webhook_data = {
                    "source": event.source or 'unknown',
                    "headers": event.headers or {},
                    "payload": alert_data
                }
                from services.ai_analyzer import analyze_webhook_with_ai, analyze_with_openclaw

                result = await analyze_with_openclaw(webhook_data, user_question)
                engine = 'openclaw'
                run_id = ''

                # 如果返回了 _degraded，说明 OpenClaw 调用失败降级到了本地规则
                if result.get('_degraded'):
                    try:
                        result = await analyze_webhook_with_ai(alert_data, event.source or 'unknown')
                        engine = 'local (fallback)'
                    except Exception as e:
                        return JSONResponse(status_code=500, content={"success": False, "error": f"深度分析失败: {e!s}"})
                elif result.get('_pending'):
                    run_id = result.get('_openclaw_run_id', '')

                # 保存分析记录
                from models import DeepAnalysis
                deep_record = DeepAnalysis(
                    webhook_event_id=webhook_id,
                    engine=engine,
                    user_question=user_question,
                    analysis_result=result,
                    status='pending' if result.get('_pending') else 'completed',
                    openclaw_run_id=run_id
                )
                session.add(deep_record)
                await session.commit()

            else:
                from models import DeepAnalysis
                from services.ai_analyzer import analyze_webhook_with_ai
                # 默认走原有的本地 AI 分析
                result = await analyze_webhook_with_ai(alert_data, event.source or 'unknown')
                engine = 'local'

                deep_record = DeepAnalysis(
                    webhook_event_id=webhook_id,
                    engine=engine,
                    user_question=user_question,
                    analysis_result=result,
                    status='completed'
                )
                session.add(deep_record)
                await session.commit()

            return JSONResponse(status_code=200, content={
                "success": True,
                "data": {
                    "id": deep_record.id,
                    "engine": engine,
                    "status": deep_record.status,
                    "result": result
                }
            })

    except Exception as e:
        logger.error(f"深度分析失败: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@deep_analysis_router.get('/api/deep-analyses')
async def list_all_deep_analyses(
    page: int = Query(1),
    per_page: int = Query(20),
    cursor: int | None = Query(None),
    status_filter: str = Query('', alias='status'),
    engine_filter: str = Query('', alias='engine')
):
    per_page = max(1, min(per_page, 100))

    async with session_scope() as session:
        # 总数（始终计算）
        total_query = select(func.count).select_from(DeepAnalysis)
        if status_filter:
            total_query = total_query.filter(DeepAnalysis.status == status_filter)
        if engine_filter:
            total_query = total_query.filter(DeepAnalysis.engine == engine_filter)
        total_res = await session.execute(total_query)
        total = total_res.scalar()

        # 记录查询
        query = select(DeepAnalysis).order_by(DeepAnalysis.id.desc())

        if cursor:
            query = query.filter(DeepAnalysis.id < cursor)
        if status_filter:
            query = query.filter(DeepAnalysis.status == status_filter)
        if engine_filter:
            query = query.filter(DeepAnalysis.engine == engine_filter)

        if not cursor:
            offset = (page - 1) * per_page
            query = query.offset(offset)

        result = await session.execute(query.limit(per_page))
        records = result.scalars().all()

        next_cursor = records[-1].id if records else None

        return {
            "success": True,
            "data": {
                "total": total,
                "page": page if not cursor else None,
                "per_page": per_page,
                "next_cursor": next_cursor,
                "items": [r.to_dict() for r in records]
            }
        }


@deep_analysis_router.get('/api/deep-analyses/{webhook_id}')
async def get_deep_analyses(webhook_id: int):
    async with session_scope() as session:
        result = await session.execute(select(DeepAnalysis).filter_by(webhook_event_id=webhook_id).order_by(DeepAnalysis.created_at.desc()))
        records = result.scalars().all()
        return {
            "success": True,
            "data": [r.to_dict() for r in records]
        }


@deep_analysis_router.post('/api/deep-analyses/{analysis_id}/forward')
async def forward_deep_analysis(analysis_id: int, payload: dict | None = None):
    payload = payload or {}
    try:
        target_url = (payload.get('target_url') or '').strip()
        if not target_url:
            return JSONResponse(status_code=400, content={'success': False, 'message': '转发 URL 不能为空'})
        if not target_url.startswith(('http://', 'https://')):
            return JSONResponse(status_code=400, content={'success': False, 'message': 'URL 格式无效'})

        async with session_scope() as session:
            analysis = await session.get(DeepAnalysis, analysis_id)
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
    """重新拉取深度分析结果"""

        # 策略：
        # - 配置了
        # - 未配置
    try:
        async with session_scope() as session:
            record = await session.get(DeepAnalysis, analysis_id)
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
                await session.commit()

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
                await session.commit()

                logger.info(f"深度分析 #{analysis_id} 已重置为 pending，等待轮询重新拉取")
                return {'success': True, 'message': '已重新开始拉取，请等待结果'}

    except Exception as e:
        logger.error(f"重试深度分析失败: {e}")
        return JSONResponse(status_code=500, content={'error': str(e)})
