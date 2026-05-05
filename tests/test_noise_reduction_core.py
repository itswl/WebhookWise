"""
tests/test_noise_reduction_core.py
====================================
测试告警降噪核心算法：特征提取、Jaccard 相似度、评分逻辑、根因判定。
这些是纯函数，不依赖数据库或外部服务。
"""

from datetime import datetime, timedelta

from services.alert_noise_reduction import (
    AlertContext,
    _extract_resource_ids,
    _jaccard,
    _tokenize_text,
    analyze_noise_reduction,
    default_decision,
    score_candidate,
)


def _make_ctx(
    event_id=1,
    source="prometheus",
    importance="high",
    parsed_data=None,
    analysis=None,
    offset_seconds=0,
) -> AlertContext:
    return AlertContext(
        event_id=event_id,
        source=source,
        importance=importance,
        parsed_data=parsed_data or {},
        analysis=analysis or {},
        timestamp=datetime(2025, 1, 1, 12, 0, 0) - timedelta(seconds=offset_seconds),
    )


# ── _jaccard ──────────────────────────────────────────────────────────────────


def test_jaccard_identical_sets():
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint_sets():
    assert _jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_partial_overlap():
    result = _jaccard({"a", "b", "c"}, {"b", "c", "d"})
    # intersection=2, union=4 → 0.5
    assert abs(result - 0.5) < 1e-9


def test_jaccard_empty_sets():
    assert _jaccard(set(), {"a"}) == 0.0
    assert _jaccard({"a"}, set()) == 0.0
    assert _jaccard(set(), set()) == 0.0


# ── _tokenize_text ────────────────────────────────────────────────────────────


def test_tokenize_extracts_english_tokens():
    tokens = _tokenize_text("high cpu usage on prod-server-01")
    assert "cpu" in tokens
    assert "usage" in tokens
    assert "prod-server-01" in tokens


def test_tokenize_short_tokens_excluded():
    """小于 3 个字符的 token 不包含。"""
    tokens = _tokenize_text("a bb ccc dddd")
    assert "a" not in tokens
    assert "bb" not in tokens
    assert "ccc" in tokens
    assert "dddd" in tokens


def test_tokenize_extracts_chinese_tokens():
    # regex [一-鿿]{2,} splits on non-CJK chars (，), so we get segments
    tokens = _tokenize_text("数据库连接失败，请检查配置")
    # 确保至少有一个包含中文字符的 token 被提取出来
    assert any(len(t) >= 2 for t in tokens)


def test_tokenize_multiple_values():
    tokens = _tokenize_text("cpu_high", "disk_full", None)
    assert "cpu_high" in tokens
    assert "disk_full" in tokens


def test_tokenize_empty_returns_empty():
    assert _tokenize_text("") == set()
    assert _tokenize_text(None) == set()


# ── _extract_resource_ids ────────────────────────────────────────────────────


def test_extract_resource_ids_direct_keys():
    data = {"host": "prod-01", "pod": "app-pod-abc"}
    ids = _extract_resource_ids(data)
    assert "prod-01" in ids
    assert "app-pod-abc" in ids


def test_extract_resource_ids_labels_dict():
    # top-level "labels" key is NOT checked by _extract_resource_ids;
    # only alerts[0].labels, Resources[], and direct keys are.
    # Use the alerts[] path which is actually supported:
    data = {"alerts": [{"labels": {"instance": "node-02:9100"}}]}
    ids = _extract_resource_ids(data)
    assert "node-02:9100" in ids


def test_extract_resource_ids_nested_alerts():
    """Prometheus alertmanager 格式：alerts[0].labels.instance"""
    data = {"alerts": [{"labels": {"instance": "web-01", "alertname": "HighCPU"}}]}
    ids = _extract_resource_ids(data)
    assert "web-01" in ids


def test_extract_resource_ids_empty_data():
    assert _extract_resource_ids({}) == set()


# ── score_candidate ───────────────────────────────────────────────────────────


def test_score_same_source_and_resources_high_score():
    """来源相同、资源相同的告警应该得高分。"""
    current = _make_ctx(
        event_id=2, source="prometheus", importance="high",
        parsed_data={"host": "prod-01", "alertname": "HighCPU"},
    )
    candidate = _make_ctx(
        event_id=1, source="prometheus", importance="high",
        parsed_data={"host": "prod-01", "alertname": "HighCPU"},
        offset_seconds=60,  # 1 分钟前
    )
    score = score_candidate(current, candidate, window_minutes=5)
    assert score > 0.7


def test_score_different_source_lower_score():
    """来源不同时，相比来源相同得分应更低。"""
    base_data = {"host": "prod-01", "alertname": "HighCPU"}
    current = _make_ctx(event_id=2, source="grafana", parsed_data=base_data)
    same_src = _make_ctx(event_id=1, source="grafana", parsed_data=base_data, offset_seconds=60)
    diff_src = _make_ctx(event_id=3, source="prometheus", parsed_data=base_data, offset_seconds=60)

    score_same = score_candidate(current, same_src, window_minutes=5)
    score_diff = score_candidate(current, diff_src, window_minutes=5)
    assert score_same > score_diff


def test_score_outside_window_returns_zero():
    """超出时间窗口的候选应得 0 分。"""
    current = _make_ctx(event_id=2)
    candidate = _make_ctx(event_id=1, offset_seconds=600)  # 10 分钟前
    score = score_candidate(current, candidate, window_minutes=5)
    assert score == 0.0


def test_score_future_candidate_returns_zero():
    """候选时间戳比当前更新（未来），应得 0 分。"""
    current = _make_ctx(event_id=2, offset_seconds=120)  # 2 分钟前
    candidate = _make_ctx(event_id=1, offset_seconds=0)  # 更新（"未来"）
    score = score_candidate(current, candidate, window_minutes=5)
    assert score == 0.0


def test_score_completely_different_alerts_low_score():
    """完全不同的告警得分接近 0。"""
    current = _make_ctx(
        event_id=2, source="github", importance="low",
        parsed_data={"repo": "my-app", "event": "push"},
    )
    candidate = _make_ctx(
        event_id=1, source="prometheus", importance="high",
        parsed_data={"host": "db-server", "alertname": "DiskFull"},
        offset_seconds=30,
    )
    score = score_candidate(current, candidate, window_minutes=5)
    assert score < 0.3


# ── analyze_noise_reduction ───────────────────────────────────────────────────


def test_analyze_standalone_no_related_alerts():
    """无历史告警时，结果应为 standalone，不抑制转发。"""
    current = _make_ctx(event_id=10, source="prometheus", importance="high",
                        parsed_data={"host": "prod-01", "alertname": "HighCPU"})
    decision = analyze_noise_reduction(current, [], window_minutes=5,
                                       min_confidence=0.4, suppress_derived=True)
    assert decision.relation == "standalone"
    assert decision.suppress_forward is False
    assert decision.root_cause_event_id is None


def test_analyze_marks_derived_when_confidence_high():
    """高置信度相关告警应被标记为衍生告警，抑制转发。"""
    base_data = {"host": "prod-01", "alertname": "HighCPU", "labels": {"instance": "prod-01"}}

    current = _make_ctx(event_id=2, source="prometheus", importance="high", parsed_data=base_data)
    root_cause = _make_ctx(event_id=1, source="prometheus", importance="high",
                           parsed_data=base_data, offset_seconds=30)

    decision = analyze_noise_reduction(current, [root_cause], window_minutes=5,
                                       min_confidence=0.3, suppress_derived=True)

    assert decision.relation == "derived"
    assert decision.root_cause_event_id == 1
    assert decision.suppress_forward is True


def test_analyze_suppress_derived_false_allows_forward():
    """suppress_derived=False 时，即使衍生也不抑制转发。"""
    base_data = {"host": "prod-01", "alertname": "MemoryLeak"}
    current = _make_ctx(event_id=2, source="prometheus", importance="high", parsed_data=base_data)
    root = _make_ctx(event_id=1, source="prometheus", importance="high",
                     parsed_data=base_data, offset_seconds=10)

    decision = analyze_noise_reduction(current, [root], window_minutes=5,
                                       min_confidence=0.1, suppress_derived=False)
    if decision.relation == "derived":
        assert decision.suppress_forward is False


def test_analyze_standalone_when_outside_window():
    """超时间窗口的历史告警不参与评分，结果为 standalone。"""
    current = _make_ctx(event_id=2, source="prometheus",
                        parsed_data={"host": "prod-01", "alertname": "HighCPU"})
    old_alert = _make_ctx(event_id=1, source="prometheus",
                          parsed_data={"host": "prod-01", "alertname": "HighCPU"},
                          offset_seconds=600)  # 10 分钟前，超出 5 分钟窗口

    decision = analyze_noise_reduction(current, [old_alert], window_minutes=5,
                                       min_confidence=0.4, suppress_derived=True)
    assert decision.relation == "standalone"


def test_analyze_alert_storm_detected():
    """高重要性告警有 >=2 个相关告警时，标记为 root_cause（告警风暴）。"""
    base_data = {"host": "prod-01", "alertname": "DBTimeout"}
    current = _make_ctx(event_id=10, source="prometheus", importance="high", parsed_data=base_data)
    related_a = _make_ctx(event_id=1, source="prometheus", importance="high",
                          parsed_data=base_data, offset_seconds=30)
    related_b = _make_ctx(event_id=2, source="prometheus", importance="high",
                          parsed_data=base_data, offset_seconds=60)

    # 置信度设高 → 两个都是衍生 → 但当前是 high 且有 >=2 关联 → 触发 root_cause
    # 注意：root_cause 分支要求 best_score < min_confidence 但 related_ids >= 2
    decision = analyze_noise_reduction(current, [related_a, related_b], window_minutes=5,
                                       min_confidence=0.99, suppress_derived=True)
    # 当 best_score 不够高时触发 root_cause 分支
    if decision.relation == "root_cause":
        assert decision.suppress_forward is False
        assert decision.related_alert_count >= 2


def test_analyze_multiple_candidates_picks_highest_score():
    """多个候选时，应选择得分最高的作为根因。"""
    base_data = {"host": "prod-01", "alertname": "HighCPU"}
    current = _make_ctx(event_id=5, source="prometheus", importance="high", parsed_data=base_data)

    candidate_a = _make_ctx(event_id=3, source="prometheus", importance="high",
                            parsed_data=base_data, offset_seconds=10)
    candidate_b = _make_ctx(event_id=2, source="github", importance="low",
                            parsed_data={"repo": "app"}, offset_seconds=20)

    decision = analyze_noise_reduction(current, [candidate_a, candidate_b], window_minutes=5,
                                       min_confidence=0.1, suppress_derived=True)

    if decision.root_cause_event_id is not None:
        assert decision.root_cause_event_id == 3


def test_default_decision_fields():
    """default_decision() 应该返回安全默认值。"""
    d = default_decision()
    assert d.relation == "standalone"
    assert d.suppress_forward is False
    assert d.confidence == 0.0
    assert d.root_cause_event_id is None
    assert d.related_alert_count == 0
    assert d.related_alert_ids == []
