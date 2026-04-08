"""
core/routes/deep_analysis.py
============================
深度分析相关路由：触发分析、列表查询、转发、重试。
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from flask import Blueprint, Response, request

from core.config import Config
from core.logger import logger
from core.models import DeepAnalysis, WebhookEvent, session_scope
from core.routes import _fail, _ok
from openai import OpenAI
from services.ai_analyzer import analyze_with_openclaw

deep_analysis_bp = Blueprint('deep_analysis', __name__)


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


def _local_ai_analysis(alert_data: dict, user_question: str) -> tuple[dict, float]:
    """本地 AI 深度分析"""
    start_time = time.time()

    if not Config.OPENAI_API_KEY:
        raise ValueError('AI 服务未配置')

    prompt_path = Path(__file__).parent.parent.parent / 'prompts' / 'deep_analysis.txt'
    try:
        with open(prompt_path, 'r', encoding='utf-8') as f:
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

    client = OpenAI(api_key=Config.OPENAI_API_KEY, base_url=Config.OPENAI_API_URL)
    response = client.chat.completions.create(
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

@deep_analysis_bp.route('/api/deep-analyze/<int:webhook_id>', methods=['POST'])
def deep_analyze_webhook(webhook_id: int) -> tuple[Response, int]:
    """
    触发深度分析（支持多引擎）

    请求体（可选）:
    {
        "user_question": "用户问题",
        "engine": "auto"  // local | openclaw | auto
    }
    """
    try:
        with session_scope() as session:
            event = session.query(WebhookEvent).filter_by(id=webhook_id).first()
            if not event:
                return _fail('Webhook not found', 404)

            alert_data = event.parsed_data or {}
            if not alert_data and event.raw_payload:
                try:
                    alert_data = json.loads(event.raw_payload)
                except (json.JSONDecodeError, TypeError):
                    alert_data = {"raw": event.raw_payload}

            payload = request.get_json(silent=True) or {}
            user_question = payload.get('user_question', '')
            requested_engine = payload.get('engine', 'auto')

            engine = _determine_engine(requested_engine)
            logger.info(f"开始深度分析 webhook ID: {webhook_id}, engine: {engine}")

            if engine == 'openclaw':
                start_time = time.time()
                webhook_data = {'source': event.source, 'parsed_data': alert_data}
                result = analyze_with_openclaw(webhook_data, user_question)
                duration = time.time() - start_time

                if result.get('_degraded'):
                    logger.warning(f"OpenClaw 分析失败，降级到本地 AI: {result.get('_degraded_reason')}")
                    try:
                        report, duration = _local_ai_analysis(alert_data, user_question)
                        engine = 'local (fallback)'
                    except Exception as e:
                        return _fail(f'深度分析失败: {str(e)}', 500)
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
                report, duration = _local_ai_analysis(alert_data, user_question)

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
                            send_feishu_deep_analysis(
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

            return _ok(
                data={
                    'analysis': report,
                    'duration_seconds': round(duration, 2),
                    'engine': engine,
                    'record_id': record_id
                }
            )

    except Exception as e:
        logger.error(f"深度分析失败: {e}", exc_info=True)
        return _fail(str(e), 500)


@deep_analysis_bp.route('/api/deep-analyses', methods=['GET'])
def list_all_deep_analyses() -> tuple[Response, int]:
    """获取所有深度分析记录（分页 + 筛选）"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    cursor = request.args.get('cursor', None, type=int)
    status_filter = request.args.get('status', '')
    engine_filter = request.args.get('engine', '')

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
        return _ok(
            data={
                'items': data,
                'total': total,
                'next_cursor': next_cursor,
                'page': page,
                'per_page': per_page,
                'total_pages': (total + per_page - 1) // per_page if total > 0 else 0,
            }
        )


@deep_analysis_bp.route('/api/deep-analyses/<int:webhook_id>', methods=['GET'])
def get_deep_analyses(webhook_id: int) -> tuple[Response, int]:
    """获取某告警的所有深度分析记录"""
    with session_scope() as session:
        records = session.query(DeepAnalysis).filter_by(
            webhook_event_id=webhook_id
        ).order_by(DeepAnalysis.created_at.desc()).all()
        return _ok(data=[r.to_dict() for r in records])


@deep_analysis_bp.route('/api/deep-analyses/<int:analysis_id>/forward', methods=['POST'])
def forward_deep_analysis(analysis_id: int):
    """转发深度分析结果到指定目标"""
    from flask import jsonify
    try:
        data = request.get_json(silent=True) or {}
        target_url = (data.get('target_url') or '').strip()
        if not target_url:
            return jsonify({'success': False, 'message': '转发 URL 不能为空'}), 400
        if not target_url.startswith(('http://', 'https://')):
            return jsonify({'success': False, 'message': 'URL 格式无效'}), 400

        with session_scope() as session:
            analysis = session.query(DeepAnalysis).get(analysis_id)
            if not analysis:
                return jsonify({'success': False, 'message': '分析记录不存在'}), 404
            if analysis.status != 'completed':
                return jsonify({'success': False, 'message': '分析尚未完成'}), 400

            source = 'unknown'
            if analysis.webhook_event_id:
                event = session.query(WebhookEvent).get(analysis.webhook_event_id)
                if event:
                    source = event.source or 'unknown'

            is_feishu = 'feishu.cn' in target_url or 'larksuite.com' in target_url

            if is_feishu:
                from adapters.ecosystem_adapters import send_feishu_deep_analysis
                success = send_feishu_deep_analysis(
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
                    return jsonify({'success': True, 'message': '已发送到飞书'})
                return jsonify({'success': False, 'message': '飞书发送失败'}), 500
            else:
                payload = {
                    'type': 'deep_analysis',
                    'analysis_id': analysis_id,
                    'source': source,
                    'engine': analysis.engine,
                    'webhook_event_id': analysis.webhook_event_id,
                    'analysis_result': analysis.analysis_result,
                    'duration_seconds': analysis.duration_seconds,
                    'created_at': analysis.created_at.isoformat() if analysis.created_at else None
                }
                resp = requests.post(target_url, json=payload, timeout=Config.FORWARD_TIMEOUT)
                resp.raise_for_status()
                return jsonify({'success': True, 'message': f'已转发 (HTTP {resp.status_code})'})
    except Exception as e:
        logger.error(f"转发深度分析失败: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@deep_analysis_bp.route('/api/deep-analyses/<int:analysis_id>/retry', methods=['POST'])
def retry_deep_analysis(analysis_id: int):
    """重新拉取深度分析结果（支持 failed 和 completed 状态）"""
    from flask import jsonify
    try:
        with session_scope() as session:
            record = session.query(DeepAnalysis).get(analysis_id)
            if not record:
                return jsonify({'error': '分析记录不存在'}), 404

            if record.status not in ('failed', 'completed'):
                return jsonify({'error': f'只能在失败或已完成状态下重新拉取，当前状态: {record.status}'}), 400

            if not record.openclaw_session_key:
                return jsonify({'error': '缺少 session key，无法重新拉取'}), 400

            record.status = 'pending'
            record.created_at = datetime.now()
            record.analysis_result = None
            session.commit()

            logger.info(f"深度分析 #{analysis_id} 已重置为 pending，等待轮询重新拉取")
            return jsonify({'success': True, 'message': '已重新开始拉取，请等待结果'})
    except Exception as e:
        logger.error(f"重试深度分析失败: {e}")
        return jsonify({'error': str(e)}), 500
