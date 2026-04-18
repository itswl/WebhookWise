"""
core/routes/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Body, Query, Response, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
import os

from core.logger import logger

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

@webhook_router.get('/api/webhooks')
def list_webhooks(
    page: int = Query(1),
    page_size: int = Query(20),
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

            total = query.count()
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
                d = event.to_dict()
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
                    'total_pages': (total + page_size - 1) // page_size if total > 0 else 0,
                    'next_cursor': next_cursor,
                    'has_more': has_more,
                }
            }
    except Exception as e:
        logger.error(f"获取 webhook 列表失败: {str(e)}", exc_info=True)
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)


@webhook_router.get('/api/webhooks/{webhook_id}')
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
    try:
        payload = await request.json()
    except:
        payload = {}
    from core.app import handle_webhook_process
    client_ip = request.client.host if request.client else "127.0.0.1"
    headers = dict(request.headers)
    background_tasks.add_task(handle_webhook_process, client_ip, headers, payload, raw_body, None)
    return JSONResponse(status_code=202, content={"success": True, "message": "Webhook received and queued for processing"})


@webhook_router.post('/webhook/{source}')
async def receive_webhook_with_source(source: str, request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    try:
        payload = await request.json()
    except:
        payload = {}
    from core.app import handle_webhook_process
    client_ip = request.client.host if request.client else "127.0.0.1"
    headers = dict(request.headers)
    background_tasks.add_task(handle_webhook_process, client_ip, headers, payload, raw_body, source)
    return JSONResponse(status_code=202, content={"success": True, "message": "Webhook received and queued for processing"})
