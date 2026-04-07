"""
core/routes/forward_rules.py
============================
转发规则 CRUD 路由。
"""
from datetime import datetime

from flask import Blueprint, request

from core.logger import logger
from core.models import ForwardRule, session_scope
from core.routes import _fail, _ok

forward_rules_bp = Blueprint('forward_rules', __name__)


# ── 路由 ─────────────────────────────────────────────────────────────────────

@forward_rules_bp.route('/api/forward-rules', methods=['GET'])
def get_forward_rules():
    """获取所有转发规则，按 priority 降序排列"""
    with session_scope() as session:
        rules = session.query(ForwardRule).order_by(ForwardRule.priority.desc()).all()
        return _ok(data=[r.to_dict() for r in rules])


@forward_rules_bp.route('/api/forward-rules', methods=['POST'])
def create_forward_rule():
    """创建转发规则"""
    payload = request.get_json(silent=True) or {}

    name = payload.get('name', '').strip()
    target_type = payload.get('target_type', '').strip()

    if not name:
        return _fail('规则名称不能为空', 400)
    if target_type not in ('feishu', 'openclaw', 'webhook'):
        return _fail('目标类型必须为 feishu/openclaw/webhook', 400)
    if target_type != 'openclaw' and not payload.get('target_url', '').strip():
        return _fail('目标地址不能为空', 400)

    with session_scope() as session:
        rule = ForwardRule(
            name=name,
            enabled=payload.get('enabled', True),
            priority=payload.get('priority', 0),
            match_importance=payload.get('match_importance', ''),
            match_duplicate=payload.get('match_duplicate', 'all'),
            match_source=payload.get('match_source', ''),
            target_type=target_type,
            target_url=payload.get('target_url', ''),
            target_name=payload.get('target_name', ''),
            stop_on_match=payload.get('stop_on_match', False)
        )
        session.add(rule)
        session.flush()
        return _ok(data=rule.to_dict(), message='规则创建成功')


@forward_rules_bp.route('/api/forward-rules/<int:rule_id>', methods=['PUT'])
def update_forward_rule(rule_id):
    """更新转发规则"""
    payload = request.get_json(silent=True) or {}

    with session_scope() as session:
        rule = session.query(ForwardRule).filter_by(id=rule_id).first()
        if not rule:
            return _fail('规则不存在', 404)

        for field in ['name', 'enabled', 'priority', 'match_importance', 'match_duplicate',
                       'match_source', 'target_type', 'target_url', 'target_name', 'stop_on_match']:
            if field in payload:
                setattr(rule, field, payload[field])

        rule.updated_at = datetime.now()
        session.flush()
        return _ok(data=rule.to_dict(), message='规则更新成功')


@forward_rules_bp.route('/api/forward-rules/<int:rule_id>', methods=['DELETE'])
def delete_forward_rule(rule_id):
    """删除转发规则"""
    with session_scope() as session:
        rule = session.query(ForwardRule).filter_by(id=rule_id).first()
        if not rule:
            return _fail('规则不存在', 404)
        session.delete(rule)
        return _ok(message='规则已删除')


@forward_rules_bp.route('/api/forward-rules/<int:rule_id>/test', methods=['POST'])
def test_forward_rule(rule_id):
    """测试转发规则（发送测试消息到目标）"""
    with session_scope() as session:
        rule = session.query(ForwardRule).filter_by(id=rule_id).first()
        if not rule:
            return _fail('规则不存在', 404)

        test_data = {
            'source': 'test',
            'parsed_data': {'message': '这是一条转发规则测试消息', 'rule_name': rule.name},
            'timestamp': datetime.now().isoformat()
        }
        test_analysis = {
            'importance': 'medium',
            'summary': f'转发规则测试 - {rule.name}',
            'event_type': 'test'
        }

        if rule.target_type == 'openclaw':
            from services.ai_analyzer import forward_to_openclaw
            result = forward_to_openclaw(test_data, test_analysis)
        else:
            from services.ai_analyzer import forward_to_remote
            result = forward_to_remote(test_data, test_analysis, target_url=rule.target_url)

        return _ok(data=result, message='测试完成')
