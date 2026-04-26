"""
core/routes/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""
from typing import Optional
from core.auth import verify_api_key
from fastapi import APIRouter, Request, HTTPException, Body, Query, Response, BackgroundTasks, Depends
from fastapi.responses import JSONResponse, FileResponse
import os
import hmac
import time

from core.logger import logger
from core.config import Config
from core.redis_client import get_redis
from core.utils import verify_signature

webhook_router = APIRouter()


# ── 健康检查 & Dashboard ────────────────────────────────────────────────────────

@webhook_router.get('/health')
def health_check():
    from core.models import test_db_connection
    db_ok = test_db_connection()
    status = 'healthy' if db_ok else 'unhealthy'
    code = 200 if db_ok else 503
    return JSONResponse(
        content={'success': True, 'data': {'status': status, 'database': 'ok' if db_ok else 'failed'}},
        status_code=code
    )


@webhook_router.get('/')
def dashboard():
    return FileResponse('templates/dashboard.html')


@webhook_router.get('/static/{filename:path}')
def serve_static(filename: str):
    static_folder = 'templates/static'
    file_path = os.path.join(static_folder, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return JSONResponse({'success': False, 'error': 'Not found'}, status_code=404)


# ── Webhooks API ────────────────────────────────────────────────────────────────

@webhook_router.get('/api/webhooks', dependencies=[Depends(verify_api_key)])
def list_webhooks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    fields: str = Query('summary'),
    include_total: bool = Query(False),
    importance: str = Query(''),
    source: str = Query(''),
    cursor_id: Optional[int] = Query(None)
):
    from core.models import WebhookEvent, session_scope

    offset = (page - 1) * page_size
    if cursor_id is not None:
        offset = 0

    try:
        with session_scope() as session:
            query = session.query(WebhookEvent).order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc())

            if importance:
                query = query.filter(WebhookEvent.importance == importance)
            if source:
                query = query.filter(WebhookEvent.source == source)
            if cursor_id is not None:
                query = query.filter(WebhookEvent.id < cursor_id)

            normalized_fields = (fields or 'summary').lower().strip()
            return_full = normalized_fields in {'full', 'all'}

            total = query.count() if include_total else None
            events = query.offset(offset).limit(page_size).all()

            # 先按 ASC 构建 prev 链，再保持原 DESC 顺序返回
            prev_ids_seen = {}
            prev_map = {}  # event.id -> prev_alert_id
            for event in reversed(events):
                h = getattr(event, 'alert_hash', '') or ''
                prev_map[event.id] = prev_ids_seen.get(h)
                prev_ids_seen[h] = event.id

            items = []
            for event in events:
                d = event.to_dict() if return_full else event.to_summary_dict()
                d['prev_alert_id'] = prev_map.get(event.id)
                beyond_window = bool(event.beyond_window)
                d['beyond_time_window'] = beyond_window
                d['is_within_window'] = bool(event.is_duplicate and not beyond_window) if event.is_duplicate else False
                items.append(d)

            has_more = len(events) == page_size
            next_cursor = events[-1].id if has_more else None

            return {
                'success': True,
                'data': items,
                'status': 200,
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total': total,
                    'total_pages': (total + page_size - 1) // page_size if (total is not None and total > 0) else (0 if total is not None else None),
                    'next_cursor': next_cursor,
                    'has_more': has_more,
                }
            }
    except Exception as e:
        logger.error(f"获取 webhook 列表失败: {str(e)}", exc_info=True)
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)


@webhook_router.get('/api/webhooks/cursor', dependencies=[Depends(verify_api_key)])
def list_webhooks_cursor(
    limit: int = Query(200, ge=1, le=500),
    fields: str = Query('summary'),
    importance: str = Query(''),
    source: str = Query(''),
    cursor_id: Optional[int] = Query(None),
):
    from core.models import WebhookEvent, session_scope

    try:
        with session_scope() as session:
            query = session.query(WebhookEvent).order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc())

            if importance:
                query = query.filter(WebhookEvent.importance == importance)
            if source:
                query = query.filter(WebhookEvent.source == source)
            if cursor_id is not None:
                query = query.filter(WebhookEvent.id < cursor_id)

            normalized_fields = (fields or 'summary').lower().strip()
            return_full = normalized_fields in {'full', 'all'}

            events = query.limit(limit).all()

            prev_ids_seen = {}
            prev_map = {}
            for event in reversed(events):
                h = getattr(event, 'alert_hash', '') or ''
                prev_map[event.id] = prev_ids_seen.get(h)
                prev_ids_seen[h] = event.id

            items = []
            for event in events:
                d = event.to_dict() if return_full else event.to_summary_dict()
                d['prev_alert_id'] = prev_map.get(event.id)
                beyond_window = bool(event.beyond_window)
                d['beyond_time_window'] = beyond_window
                d['is_within_window'] = bool(event.is_duplicate and not beyond_window) if event.is_duplicate else False
                items.append(d)

            has_more = len(events) == limit
            next_cursor = events[-1].id if has_more else None

            return {
                'success': True,
                'data': items,
                'status': 200,
                'cursor': {
                    'limit': limit,
                    'next_cursor': next_cursor,
                    'has_more': has_more,
                }
            }
    except Exception as e:
        logger.error(f"获取 webhook 游标列表失败: {str(e)}", exc_info=True)
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)


@webhook_router.get('/api/webhooks/{webhook_id}', dependencies=[Depends(verify_api_key)])
def get_webhook_detail(webhook_id: int):
    from core.models import WebhookEvent, session_scope

    with session_scope() as session:
        event = session.query(WebhookEvent).filter_by(id=webhook_id).first()
        if not event:
            return JSONResponse({'success': False, 'error': 'Webhook not found'}, status_code=404)
        return {'success': True, 'data': event.to_dict()}


# ── Webhook 接收 ───────────────────────────────────────────────────────────────

@webhook_router.post('/webhook')
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    if Config.MAX_WEBHOOK_BODY_BYTES and len(raw_body) > Config.MAX_WEBHOOK_BODY_BYTES:
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})
    try:
        payload = await request.json()
    except:
        payload = {}
    from core.app import handle_webhook_process
    client_ip = request.client.host if request.client else "127.0.0.1"
    headers = dict(request.headers)

    if Config.WEBHOOK_RATE_LIMIT_PER_MINUTE and Config.WEBHOOK_RATE_LIMIT_PER_MINUTE > 0:
        try:
            redis = get_redis()
            window = int(time.time() // 60)
            key = f"rl:webhook:{client_ip}:{window}"
            current = redis.incr(key)
            if current == 1:
                redis.expire(key, 70)
            if current > Config.WEBHOOK_RATE_LIMIT_PER_MINUTE:
                return JSONResponse(status_code=429, content={"success": False, "error": "Rate limit exceeded"})
        except Exception as e:
            logger.warning(f"限流检查失败: {e}")

    signature = headers.get('x-webhook-signature', '')
    token = headers.get('token', '')
    if not token and headers.get('authorization', '').startswith('Token '):
        token = headers.get('authorization', '')[6:].strip()

    if signature:
        if not verify_signature(raw_body, signature):
            return JSONResponse(status_code=401, content={"success": False, "error": "Invalid signature"})
    elif Config.WEBHOOK_SECRET:
        if not token:
            return JSONResponse(status_code=401, content={"success": False, "error": "Missing token"})
        if not hmac.compare_digest(token, Config.WEBHOOK_SECRET):
            return JSONResponse(status_code=401, content={"success": False, "error": "Invalid token"})

    background_tasks.add_task(handle_webhook_process, client_ip, headers, payload, raw_body, None)
    return JSONResponse(status_code=202, content={"success": True, "message": "Webhook received and queued for processing"})


@webhook_router.post('/webhook/{source}')
async def receive_webhook_with_source(source: str, request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    if Config.MAX_WEBHOOK_BODY_BYTES and len(raw_body) > Config.MAX_WEBHOOK_BODY_BYTES:
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})
    try:
        payload = await request.json()
    except:
        payload = {}
    from core.app import handle_webhook_process
    client_ip = request.client.host if request.client else "127.0.0.1"
    headers = dict(request.headers)

    if Config.WEBHOOK_RATE_LIMIT_PER_MINUTE and Config.WEBHOOK_RATE_LIMIT_PER_MINUTE > 0:
        try:
            redis = get_redis()
            window = int(time.time() // 60)
            key = f"rl:webhook:{client_ip}:{window}"
            current = redis.incr(key)
            if current == 1:
                redis.expire(key, 70)
            if current > Config.WEBHOOK_RATE_LIMIT_PER_MINUTE:
                return JSONResponse(status_code=429, content={"success": False, "error": "Rate limit exceeded"})
        except Exception as e:
            logger.warning(f"限流检查失败: {e}")

    signature = headers.get('x-webhook-signature', '')
    token = headers.get('token', '')
    if not token and headers.get('authorization', '').startswith('Token '):
        token = headers.get('authorization', '')[6:].strip()

    if signature:
        if not verify_signature(raw_body, signature):
            return JSONResponse(status_code=401, content={"success": False, "error": "Invalid signature"})
    elif Config.WEBHOOK_SECRET:
        if not token:
            return JSONResponse(status_code=401, content={"success": False, "error": "Missing token"})
        if not hmac.compare_digest(token, Config.WEBHOOK_SECRET):
            return JSONResponse(status_code=401, content={"success": False, "error": "Invalid token"})

    background_tasks.add_task(handle_webhook_process, client_ip, headers, payload, raw_body, source)
    return JSONResponse(status_code=202, content={"success": True, "message": "Webhook received and queued for processing"})
