from datetime import datetime, timedelta

from alert_noise_reduction import AlertContext, analyze_noise_reduction, score_candidate


def _ctx(
    event_id: int,
    *,
    source: str = 'cloud-monitor',
    importance: str = 'high',
    rule_name: str = '数据库连接超时',
    instance: str = 'db-1',
    summary: str = '数据库超时导致服务异常',
    minutes_ago: int = 1,
):
    now = datetime.now()
    parsed_data = {
        'RuleName': rule_name,
        'Resources': [{'InstanceId': instance}],
        'Level': 'critical' if importance == 'high' else 'warning',
    }
    analysis = {
        'importance': importance,
        'summary': summary,
        'event_type': rule_name,
    }
    return AlertContext(
        event_id=event_id,
        source=source,
        importance=importance,
        parsed_data=parsed_data,
        analysis=analysis,
        timestamp=now - timedelta(minutes=minutes_ago),
        alert_hash=f'hash-{event_id}',
    )


def test_score_candidate_higher_for_similar_alerts():
    current = _ctx(999, rule_name='API 5xx激增', instance='api-1', summary='服务报错激增', minutes_ago=0)
    similar = _ctx(1, rule_name='API 5xx激增', instance='api-1', summary='服务报错', minutes_ago=1)
    unrelated = _ctx(2, source='other', rule_name='磁盘告警', instance='node-8', summary='磁盘空间不足', minutes_ago=1)

    similar_score = score_candidate(current, similar, window_minutes=5)
    unrelated_score = score_candidate(current, unrelated, window_minutes=5)

    assert similar_score > unrelated_score
    assert similar_score > 0.5


def test_noise_reduction_marks_derived_when_confidence_high():
    current = _ctx(1000, rule_name='服务A响应慢', instance='app-1', summary='可能由数据库异常引发', minutes_ago=0)
    root = _ctx(10, rule_name='数据库连接超时', instance='app-1', summary='数据库超时', minutes_ago=1)
    peer = _ctx(11, rule_name='服务B报错', instance='app-1', summary='服务报错', minutes_ago=2)

    decision = analyze_noise_reduction(
        current,
        [root, peer],
        window_minutes=5,
        min_confidence=0.4,
        suppress_derived=True,
    )

    assert decision.relation == 'derived'
    assert decision.root_cause_event_id in {10, 11}
    assert decision.suppress_forward is True
    assert decision.confidence >= 0.4


def test_noise_reduction_standalone_when_no_related_alerts():
    current = _ctx(2000, rule_name='服务A响应慢', instance='app-1', summary='服务慢', minutes_ago=0)
    old_alert = _ctx(21, rule_name='数据库连接超时', instance='app-1', summary='数据库超时', minutes_ago=20)

    decision = analyze_noise_reduction(
        current,
        [old_alert],
        window_minutes=5,
        min_confidence=0.4,
        suppress_derived=True,
    )

    assert decision.relation == 'standalone'
    assert decision.root_cause_event_id is None
    assert decision.suppress_forward is False
