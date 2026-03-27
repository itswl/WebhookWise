from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import logging
from typing import Any, Iterable, Optional, List, Tuple

logger = logging.getLogger(__name__)


AlertPayload = dict[str, Any]
AnalysisResult = dict[str, Any]


@dataclass(frozen=True)
class AlertContext:
    event_id: Optional[int]
    source: str
    importance: str
    parsed_data: AlertPayload
    analysis: AnalysisResult
    timestamp: datetime
    alert_hash: Optional[str] = None


@dataclass(frozen=True)
class NoiseReductionDecision:
    relation: str
    root_cause_event_id: Optional[int]
    confidence: float
    suppress_forward: bool
    reason: str
    related_alert_count: int
    related_alert_ids: list[int]


def default_decision() -> NoiseReductionDecision:
    return NoiseReductionDecision(
        relation='standalone',
        root_cause_event_id=None,
        confidence=0.0,
        suppress_forward=False,
        reason='未发现可关联的告警关系',
        related_alert_count=0,
        related_alert_ids=[]
    )


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _tokenize_text(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).lower().strip()
        if not text:
            continue

        # 英文/数字 token
        for token in re.findall(r'[a-z0-9_.-]{3,}', text):
            tokens.add(token)

        # 简单中文片段 token
        for token in re.findall(r'[\u4e00-\u9fff]{2,}', text):
            tokens.add(token)

    return tokens


def _pick_first(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_resource_ids(parsed_data: AlertPayload) -> set[str]:
    ids: set[str] = set()

    direct_keys = ['resource_id', 'ResourceID', 'InstanceId', 'instance', 'host', 'pod']
    for key in direct_keys:
        value = parsed_data.get(key)
        if value:
            ids.add(str(value).strip().lower())

    resources = _safe_list(parsed_data.get('Resources'))
    for item in resources:
        if not isinstance(item, dict):
            continue
        candidate = _pick_first(item.get('InstanceId'), item.get('Id'), item.get('id'))
        if candidate:
            ids.add(candidate.lower())

    alerts = _safe_list(parsed_data.get('alerts'))
    if alerts:
        first_alert = alerts[0] if isinstance(alerts[0], dict) else {}
        labels = _safe_dict(first_alert.get('labels'))
        for key in ('instance', 'pod', 'host', 'service', 'namespace'):
            value = labels.get(key)
            if value:
                ids.add(str(value).strip().lower())

    return {x for x in ids if x}


def _extract_features(ctx: AlertContext) -> tuple[set[str], set[str]]:
    parsed = _safe_dict(ctx.parsed_data)
    analysis = _safe_dict(ctx.analysis)

    resource_ids = _extract_resource_ids(parsed)

    primary_fields = [
        parsed.get('RuleName'),
        parsed.get('alert_name'),
        parsed.get('event_type'),
        parsed.get('event'),
        parsed.get('MetricName'),
        parsed.get('Type'),
        parsed.get('service'),
        analysis.get('event_type'),
        analysis.get('summary'),
        analysis.get('root_cause'),
        analysis.get('impact_scope')
    ]

    alerts = _safe_list(parsed.get('alerts'))
    if alerts:
        first_alert = alerts[0] if isinstance(alerts[0], dict) else {}
        labels = _safe_dict(first_alert.get('labels'))
        annotations = _safe_dict(first_alert.get('annotations'))
        primary_fields.extend([
            labels.get('alertname'),
            labels.get('severity'),
            labels.get('service'),
            annotations.get('summary'),
            annotations.get('description')
        ])

    tokens = _tokenize_text(*primary_fields)
    return resource_ids, tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union_size = len(a | b)
    if union_size == 0:
        return 0.0
    return len(a & b) / union_size


def _importance_score(value: str) -> float:
    mapping = {'high': 1.0, 'medium': 0.6, 'low': 0.2}
    return mapping.get(str(value).lower(), 0.6)


def score_candidate(current: AlertContext, candidate: AlertContext, window_minutes: int) -> float:
    if candidate.timestamp > current.timestamp:
        return 0.0

    elapsed = (current.timestamp - candidate.timestamp).total_seconds()
    window_seconds = max(window_minutes, 1) * 60
    if elapsed > window_seconds:
        return 0.0

    current_resources, current_tokens = _extract_features(current)
    candidate_resources, candidate_tokens = _extract_features(candidate)

    source_score = 0.15 if current.source == candidate.source else 0.0
    resource_score = 0.45 * _jaccard(current_resources, candidate_resources)
    token_score = 0.25 * _jaccard(current_tokens, candidate_tokens)

    candidate_level = _importance_score(candidate.importance)
    current_level = _importance_score(current.importance)
    severity_score = 0.1 if candidate_level >= current_level else 0.03

    time_score = 0.2 * (1 - (elapsed / window_seconds))

    total = source_score + resource_score + token_score + severity_score + time_score
    if total < 0:
        return 0.0
    if total > 1:
        return 1.0
    return total


def _collect_related(
    current: AlertContext,
    recent_alerts: Iterable[AlertContext],
    window_minutes: int,
) -> list[tuple[AlertContext, float]]:
    scored: list[tuple[AlertContext, float]] = []
    for alert in recent_alerts:
        score = score_candidate(current, alert, window_minutes)
        if score > 0:
            scored.append((alert, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def analyze_noise_reduction(
    current: AlertContext,
    recent_alerts: Iterable[AlertContext],
    *,
    window_minutes: int,
    min_confidence: float,
    suppress_derived: bool,
    use_dynamic_threshold: bool = True,
    session: Any = None,
) -> NoiseReductionDecision:
    """
    分析噪声降低
    
    增强功能：
    - 动态阈值：基于历史数据自动调整判定阈值
    
    Args:
        current: 当前告警上下文
        recent_alerts: 近期告警列表
        window_minutes: 时间窗口（分钟）
        min_confidence: 最小置信度阈值
        suppress_derived: 是否抑制衍生告警转发
        use_dynamic_threshold: 是否使用动态阈值
        session: 数据库会话（可选）
    """
    # 使用动态阈值
    effective_threshold = min_confidence
    if use_dynamic_threshold:
        try:
            dynamic_threshold = calculate_dynamic_threshold(
                session=session,
                lookback_hours=24,
                base_threshold=min_confidence
            )
            effective_threshold = dynamic_threshold
            logger.debug(f"使用动态阈值: {effective_threshold:.4f}")
        except Exception as e:
            logger.warning(f"计算动态阈值失败，使用默认值: {e}")
    
    # 收集相关告警
    recent_alerts_list = list(recent_alerts)
    scored = _collect_related(current, recent_alerts_list, window_minutes)
    
    if not scored:
        return default_decision()

    related = [(alert, score) for alert, score in scored if score >= 0.35]
    related_ids = [alert.event_id for alert, _ in related if alert.event_id is not None]

    best_alert, best_score = scored[0]
    
    # 根因判定
    if best_alert.event_id is not None and best_score >= effective_threshold:
        reason = f'与告警#{best_alert.event_id} 高相关（置信度 {best_score:.2f}）'
        
        return NoiseReductionDecision(
            relation='derived',
            root_cause_event_id=best_alert.event_id,
            confidence=round(best_score, 4),
            suppress_forward=suppress_derived,
            reason=reason,
            related_alert_count=len(related_ids),
            related_alert_ids=related_ids,
        )

    # 告警风暴检测
    if current.importance == 'high' and len(related_ids) >= 2:
        reason = f'检测到告警风暴，已关联 {len(related_ids)} 条近邻告警'
        
        return NoiseReductionDecision(
            relation='root_cause',
            root_cause_event_id=current.event_id,
            confidence=round(best_score, 4),
            suppress_forward=False,
            reason=reason,
            related_alert_count=len(related_ids),
            related_alert_ids=related_ids,
        )

    return NoiseReductionDecision(
        relation='standalone',
        root_cause_event_id=None,
        confidence=round(best_score, 4),
        suppress_forward=False,
        reason='存在弱关联告警，但未达到根因判定阈值',
        related_alert_count=len(related_ids),
        related_alert_ids=related_ids,
    )


def get_historical_weight(
    alert_hash_a: str,
    alert_hash_b: str,
    session: Any = None
) -> float:
    """
    获取历史频率权重
    
    查询 AlertCorrelation 表，如果两个告警经常一起出现则权重更高
    
    Args:
        alert_hash_a: 告警 A 的哈希
        alert_hash_b: 告警 B 的哈希
        session: 数据库会话（可选）
        
    Returns:
        float: 历史权重 (0.0 - 0.25)
    """
    if not alert_hash_a or not alert_hash_b:
        return 0.0
    
    try:
        if not session:
            from core.models import get_session
            session = get_session()
            should_close = True
        else:
            should_close = False
        
        try:
            from core.models import AlertCorrelation
            
            # 查询两个哈希的关联记录（顺序无关）
            correlation = session.query(AlertCorrelation).filter(
                ((AlertCorrelation.alert_hash_a == alert_hash_a) & 
                 (AlertCorrelation.alert_hash_b == alert_hash_b)) |
                ((AlertCorrelation.alert_hash_a == alert_hash_b) & 
                 (AlertCorrelation.alert_hash_b == alert_hash_a))
            ).first()
            
            if not correlation:
                return 0.0
            
            # 基于共现次数和置信度计算权重
            # 共现 10 次以上达到最大权重 0.25
            co_occurrence_factor = min(1.0, correlation.co_occurrence_count / 10)
            confidence_factor = correlation.confidence or 0.0
            
            weight = 0.25 * (0.6 * co_occurrence_factor + 0.4 * confidence_factor)
            
            logger.debug(f"历史权重: {alert_hash_a[:8]}... <-> {alert_hash_b[:8]}..., "
                        f"共现={correlation.co_occurrence_count}, 权重={weight:.4f}")
            
            return round(weight, 4)
            
        finally:
            if should_close:
                session.close()
                
    except Exception as e:
        logger.warning(f"获取历史权重失败: {e}")
        return 0.0


def update_alert_correlation(
    alert_hash_a: str,
    alert_hash_b: str,
    time_delta: float,
    confidence: float,
    session: Any = None
) -> bool:
    """
    更新告警关联记录
    
    Args:
        alert_hash_a: 告警 A 的哈希
        alert_hash_b: 告警 B 的哈希
        time_delta: 时间差（秒），A 在 B 之前为正
        confidence: 关联置信度
        session: 数据库会话
        
    Returns:
        bool: 是否更新成功
    """
    if not alert_hash_a or not alert_hash_b:
        return False
    
    try:
        if not session:
            from core.models import get_session
            session = get_session()
            should_close = True
        else:
            should_close = False
        
        try:
            from core.models import AlertCorrelation
            from sqlalchemy import func
            
            # 确保 hash_a < hash_b 以保持一致性
            if alert_hash_a > alert_hash_b:
                alert_hash_a, alert_hash_b = alert_hash_b, alert_hash_a
                time_delta = -time_delta
            
            # 查询现有记录
            correlation = session.query(AlertCorrelation).filter(
                AlertCorrelation.alert_hash_a == alert_hash_a,
                AlertCorrelation.alert_hash_b == alert_hash_b
            ).first()
            
            if correlation:
                # 更新现有记录
                # 使用增量平均计算 avg_time_delta
                old_count = correlation.co_occurrence_count
                new_count = old_count + 1
                correlation.avg_time_delta = (
                    (correlation.avg_time_delta * old_count + time_delta) / new_count
                )
                correlation.co_occurrence_count = new_count
                # 更新置信度（取较大值）
                correlation.confidence = max(correlation.confidence, confidence)
                correlation.last_seen = func.now()
            else:
                # 创建新记录
                correlation = AlertCorrelation(
                    alert_hash_a=alert_hash_a,
                    alert_hash_b=alert_hash_b,
                    co_occurrence_count=1,
                    avg_time_delta=time_delta,
                    confidence=confidence
                )
                session.add(correlation)
            
            session.commit()
            logger.debug(f"更新告警关联: {alert_hash_a[:8]}... <-> {alert_hash_b[:8]}...")
            return True
            
        finally:
            if should_close:
                session.close()
                
    except Exception as e:
        logger.warning(f"更新告警关联失败: {e}")
        return False


def calculate_dynamic_threshold(
    session: Any = None,
    lookback_hours: int = 24,
    base_threshold: float = 0.65
) -> float:
    """
    动态计算根因判定阈值
    
    基于近期告警的平均相似度调整阈值
    
    Args:
        session: 数据库会话
        lookback_hours: 回溯时间（小时）
        base_threshold: 基础阈值
        
    Returns:
        float: 动态阈值 (0.5 - 0.8)
    """
    try:
        if not session:
            from core.models import get_session
            session = get_session()
            should_close = True
        else:
            should_close = False
        
        try:
            from core.models import AlertCorrelation
            from sqlalchemy import func
            
            # 查询近期的关联记录
            time_threshold = datetime.now() - timedelta(hours=lookback_hours)
            
            stats = session.query(
                func.avg(AlertCorrelation.confidence).label('avg_confidence'),
                func.count(AlertCorrelation.id).label('count')
            ).filter(
                AlertCorrelation.last_seen >= time_threshold
            ).first()
            
            if not stats or not stats.count or stats.count < 5:
                logger.debug(f"动态阈值: 数据不足，使用基础阈值 {base_threshold}")
                return base_threshold
            
            avg_confidence = stats.avg_confidence or base_threshold
            
            # 根据平均置信度调整阈值
            # 如果平均置信度高，提高阈值以减少误判
            # 如果平均置信度低，降低阈值以提高敏感度
            dynamic_threshold = base_threshold + 0.1 * (avg_confidence - 0.5)
            
            # 限制范围
            dynamic_threshold = max(0.5, min(0.8, dynamic_threshold))
            
            logger.debug(f"动态阈值: avg_confidence={avg_confidence:.2f}, "
                        f"threshold={dynamic_threshold:.2f}")
            
            return round(dynamic_threshold, 4)
            
        finally:
            if should_close:
                session.close()
                
    except Exception as e:
        logger.warning(f"计算动态阈值失败: {e}")
        return base_threshold


