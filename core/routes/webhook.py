"""
core/routes/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""
from flask import Blueprint, Response, request, render_template

from core.logger import logger
from core.routes import _ok, _fail

webhook_bp = Blueprint('webhook', __name__)


# ── 健康检查 & Dashboard ────────────────────────────────────────────────────────

@webhook_bp.route('/health', methods=['GET'])
def health_check():
    from core.models import test_db_connection
    db_ok = test_db_connection()
    status = 'healthy' if db_ok else 'unhealthy'
    return _ok({'status': status, 'database': 'ok' if db_ok else 'failed'}, 200 if db_ok else 503)


@webhook_bp.route('/', methods=['GET'])
def dashboard():
    return render_template('dashboard.html')


@webhook_bp.route('/static/<path:filename>', methods=['GET'])
def serve_static(filename):
    from flask import send_from_directory
    from core.app import app
    static_folder = app.static_folder or 'templates/static'
    return send_from_directory(static_folder, filename)


# ── Webhooks API ────────────────────────────────────────────────────────────────

@webhook_bp.route('/api/webhooks', methods=['GET'])
def list_webhooks() -> tuple[Response, int]:
    from core.models import WebhookEvent, session_scope

    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    importance = request.args.get('importance', '')
    source = request.args.get('source', '')
    cursor_id = request.args.get('cursor_id', None, type=int)

    offset = (page - 1) * page_size
    if cursor_id is not None:
        offset = 0

    try:
        with session_scope() as session:
            query = session.query(WebhookEvent).order_by(WebhookEvent.id.desc())

            if importance:
                query = query.filter(WebhookEvent.importance == importance)
            if source:
                query = query.filter(WebhookEvent.source == source)
            if cursor_id is not None:
                query = query.filter(WebhookEvent.id < cursor_id)

            total = query.count()
            events = query.offset(offset).limit(page_size).all()

            # 批量计算 prev_alert_id
            prev_map = {}
            if events:
                all_hashes = [getattr(e, 'alert_hash', '') or '' for e in events]
                all_alerts = session.query(
                    WebhookEvent.id, WebhookEvent.alert_hash, WebhookEvent.timestamp
                ).filter(
                    WebhookEvent.alert_hash.in_(all_hashes),
                    WebhookEvent.id < events[0].id
                ).all()
                for a in all_alerts:
                    h = getattr(a, 'alert_hash', '') or ''
                    if h not in prev_map or a.id > prev_map[h][0]:
                        prev_map[h] = (a.id, a.timestamp)

            items = []
            for event in events:
                d = event.to_dict()
                h = getattr(event, 'alert_hash', '') or ''
                d['prev_alert_id'] = prev_map.get(h, (None, None))[0] if h in prev_map else None
                items.append(d)

            has_more = len(events) == page_size
            next_cursor = events[-1].id if has_more else None

            return _ok(
                items,
                status=200,
                pagination={
                    'page': page,
                    'page_size': page_size,
                    'total': total,
                    'total_pages': (total + page_size - 1) // page_size if total > 0 else 0,
                    'next_cursor': next_cursor,
                    'has_more': has_more,
                }
            )
    except Exception as e:
        logger.error(f"获取 webhook 列表失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)


@webhook_bp.route('/api/webhooks/<int:webhook_id>', methods=['GET'])
def get_webhook_detail(webhook_id: int) -> tuple[Response, int]:
    from core.models import WebhookEvent, session_scope

    with session_scope() as session:
        event = session.query(WebhookEvent).filter_by(id=webhook_id).first()
        if not event:
            return _fail('Webhook not found', 404)
        return _ok(event.to_dict())


# ── Webhook 接收 ───────────────────────────────────────────────────────────────

@webhook_bp.route('/webhook', methods=['POST'])
def receive_webhook():
    from core.app import handle_webhook_process
    return handle_webhook_process(None)


@webhook_bp.route('/webhook/<source>', methods=['POST'])
def receive_webhook_with_source(source: str):
    from core.app import handle_webhook_process
    return handle_webhook_process(source)


# ── 错误处理 ──────────────────────────────────────────────────────────────────

@webhook_bp.errorhandler(404)
def not_found(_error):
    return {'status': 'error', 'error': 'Not found'}, 404


@webhook_bp.errorhandler(405)
def method_not_allowed(_error):
    return {'status': 'error', 'error': 'Method not allowed'}, 405
