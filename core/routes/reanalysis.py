"""
core/routes/reanalysis.py
=========================
重新分析 + 手动转发路由。
"""
from typing import Optional

from flask import Blueprint, Response

from core.config import Config
from core.logger import logger
from core.models import WebhookEvent, session_scope
from core.routes import _fail, _ok
from services.ai_analyzer import analyze_webhook_with_ai, forward_to_remote

reanalysis_bp = Blueprint('reanalysis', __name__)


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def _get_webhook_event_by_id(session, webhook_id: int) -> Optional[WebhookEvent]:
    return session.query(WebhookEvent).filter_by(id=webhook_id).first()


def _build_webhook_context(event: WebhookEvent) -> dict:
    return {
        'source': event.source,
        'parsed_data': event.parsed_data,
        'timestamp': event.timestamp.isoformat() if event.timestamp else None,
        'client_ip': event.client_ip
    }


def _propagate_analysis_to_duplicates(session, webhook_id: int, analysis_result: dict, new_importance: Optional[str]) -> int:
    duplicate_events = session.query(WebhookEvent).filter(WebhookEvent.duplicate_of == webhook_id).all()
    for dup in duplicate_events:
        dup.ai_analysis = analysis_result
        dup.importance = new_importance
    return len(duplicate_events)


def _reanalyze_webhook_event(session, webhook_event: WebhookEvent, webhook_id: int) -> tuple[dict, Optional[str], Optional[str], int]:
    webhook_data = _build_webhook_context(webhook_event)

    logger.info(f"重新分析 webhook ID: {webhook_id}")
    analysis_result = analyze_webhook_with_ai(webhook_data, skip_cache=True)

    old_importance = webhook_event.importance
    new_importance = analysis_result.get('importance')

    webhook_event.ai_analysis = analysis_result
    webhook_event.importance = new_importance

    logger.info(f"重新分析完成: {old_importance} → {new_importance} - {analysis_result.get('summary', '')}")

    updated_duplicates = 0
    if webhook_event.is_duplicate == 0:
        updated_duplicates = _propagate_analysis_to_duplicates(session, webhook_id, analysis_result, new_importance)
        if updated_duplicates:
            logger.info(f"同时更新了 {updated_duplicates} 条重复告警的分析结果")

    return analysis_result, old_importance, new_importance, updated_duplicates


def _manual_forward(session, webhook_event: WebhookEvent, webhook_id: int, custom_url) -> dict:
    webhook_data = _build_webhook_context(webhook_event)
    analysis_result = webhook_event.ai_analysis or {}

    logger.info(f"手动转发 webhook ID: {webhook_id} 到 {custom_url or Config.FORWARD_URL}")
    forward_result = forward_to_remote(webhook_data, analysis_result, custom_url)

    webhook_event.forward_status = forward_result.get('status', 'unknown')
    return forward_result


# ── 路由 ─────────────────────────────────────────────────────────────────────

@reanalysis_bp.route('/api/reanalyze/<int:webhook_id>', methods=['POST'])
def reanalyze_webhook(webhook_id: int) -> tuple[Response, int]:
    """重新分析指定的 webhook，并更新所有引用它的重复告警"""
    try:
        with session_scope() as session:
            webhook_event = _get_webhook_event_by_id(session, webhook_id)
            if not webhook_event:
                return _fail('Webhook not found', 404)

            analysis_result, old_importance, new_importance, updated_duplicates = _reanalyze_webhook_event(
                session, webhook_event, webhook_id
            )

            return _ok(
                status=200,
                analysis=analysis_result,
                original_importance=old_importance,
                new_importance=new_importance,
                updated_duplicates=updated_duplicates,
                message=f'重新分析完成，importance: {old_importance} → {new_importance}' +
                        (f'，同时更新了 {updated_duplicates} 条重复告警' if updated_duplicates > 0 else '')
            )

    except Exception as e:
        logger.error(f"重新分析失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)


@reanalysis_bp.route('/api/forward/<int:webhook_id>', methods=['POST'])
def manual_forward_webhook(webhook_id: int) -> tuple[Response, int]:
    """手动转发 webhook"""
    from flask import request
    try:
        data = request.get_json(silent=True) or {}
        custom_url = data.get('target_url')

        with session_scope() as session:
            webhook_event = _get_webhook_event_by_id(session, webhook_id)
            if not webhook_event:
                return _fail('Webhook not found', 404)

            forward_result = _manual_forward(session, webhook_event, webhook_id, custom_url)

            return _ok(
                data=forward_result,
                message=f"已转发至 {custom_url or Config.FORWARD_URL}"
            )
    except Exception as e:
        logger.error(f"手动转发失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)
