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


def _collect_related_enhanced(
    current: AlertContext,
    recent_alerts: Iterable[AlertContext],
    window_minutes: int,
    topology_manager: Any = None,
    session: Any = None
) -> list[tuple[AlertContext, float]]:
    """增强版收集相关告警函数"""
    scored: list[tuple[AlertContext, float]] = []
    for alert in recent_alerts:
        score = score_candidate_enhanced(
            current, alert, window_minutes, topology_manager, session
        )
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
    use_topology: bool = True,
    use_dynamic_threshold: bool = True,
    session: Any = None,
) -> NoiseReductionDecision:
    """
    分析噪声降低
    
    增强功能：
    - 拓扑感知：利用服务依赖关系增强关联判断
    - 动态阈值：基于历史数据自动调整判定阈值
    - 传播路径推理：识别告警传播路径和根因
    
    Args:
        current: 当前告警上下文
        recent_alerts: 近期告警列表
        window_minutes: 时间窗口（分钟）
        min_confidence: 最小置信度阈值
        suppress_derived: 是否抑制衍生告警转发
        use_topology: 是否使用拓扑感知
        use_dynamic_threshold: 是否使用动态阈值
        session: 数据库会话（可选）
    """
    # 获取拓扑管理器
    topology_manager = None
    if use_topology:
        topology_manager = _get_topology_manager()
    
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
    
    # 收集相关告警（使用增强版或基础版）
    recent_alerts_list = list(recent_alerts)
    
    if use_topology and topology_manager:
        scored = _collect_related_enhanced(
            current, recent_alerts_list, window_minutes, topology_manager, session
        )
    else:
        scored = _collect_related(current, recent_alerts_list, window_minutes)
    
    if not scored:
        return default_decision()

    related = [(alert, score) for alert, score in scored if score >= 0.35]
    related_ids = [alert.event_id for alert, _ in related if alert.event_id is not None]

    best_alert, best_score = scored[0]
    
    # 推理传播路径（用于增强决策）
    propagation_paths = []
    if use_topology and topology_manager and len(recent_alerts_list) >= 2:
        try:
            all_alerts = [current] + recent_alerts_list
            propagation_paths = infer_propagation_path(all_alerts, topology_manager)
        except Exception as e:
            logger.warning(f"推理传播路径失败: {e}")
    
    # 根因判定
    if best_alert.event_id is not None and best_score >= effective_threshold:
        reason = f'与告警#{best_alert.event_id} 高相关（置信度 {best_score:.2f}）'
        
        # 如果有传播路径信息，增强原因描述
        if propagation_paths:
            top_path = propagation_paths[0]
            if best_alert.event_id in top_path.get('root_cause_alerts', []):
                reason = (f'根因推理: {top_path["root_cause_service"]} → '
                         f'{top_path["affected_service"]} '
                         f'（置信度 {best_score:.2f}）')
        
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
        
        # 如果有传播路径，标记可能的根因
        if propagation_paths:
            top_path = propagation_paths[0]
            reason = (f'告警风暴根因分析: 源自 {top_path["root_cause_service"]}，'
                     f'已关联 {len(related_ids)} 条告警')
        
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


# =============================================================================
# 拓扑感知增强功能
# =============================================================================

def _get_topology_manager():
    """延迟导入 topology_manager 以避免循环导入"""
    try:
        from services.topology import topology_manager
        return topology_manager
    except ImportError:
        logger.warning("无法导入 topology_manager，拓扑功能将不可用")
        return None


def _get_service_from_alert(ctx: AlertContext) -> str:
    """从告警上下文中提取服务名称"""
    # 优先从 source 获取
    service = ctx.source.strip().lower() if ctx.source else ''
    
    # 尝试从 parsed_data 中提取更具体的服务名
    parsed = _safe_dict(ctx.parsed_data)
    
    # 检查常见的服务标识字段
    service_fields = ['service', 'Service', 'app', 'application', 'component']
    for field in service_fields:
        value = parsed.get(field)
        if value:
            return str(value).strip().lower()
    
    # 检查 Prometheus 格式的 labels
    alerts = _safe_list(parsed.get('alerts'))
    if alerts:
        first_alert = alerts[0] if isinstance(alerts[0], dict) else {}
        labels = _safe_dict(first_alert.get('labels'))
        for field in ['service', 'app', 'job', 'namespace']:
            value = labels.get(field)
            if value:
                return str(value).strip().lower()
    
    return service or 'unknown'


def calculate_topology_weight(
    current: AlertContext,
    candidate: AlertContext,
    topology_manager: Any
) -> float:
    """
    计算两个告警之间的拓扑权重
    
    规则：
    - 上下游关系: +0.3
    - 同一服务: +0.2
    - 共同上游（兄弟服务）: +0.15
    - 无关系: 0
    
    Args:
        current: 当前告警上下文
        candidate: 候选告警上下文
        topology_manager: 拓扑管理器实例
        
    Returns:
        float: 拓扑权重 (0.0 - 0.3)
    """
    if not topology_manager:
        return 0.0
    
    try:
        current_service = _get_service_from_alert(current)
        candidate_service = _get_service_from_alert(candidate)
        
        if not current_service or not candidate_service:
            return 0.0
        
        # 同一服务
        if current_service == candidate_service:
            logger.debug(f"拓扑权重: 同一服务 ({current_service}), +0.2")
            return 0.2
        
        is_related, relationship = topology_manager.are_related(
            current_service, candidate_service
        )
        
        if not is_related:
            return 0.0
        
        if relationship in ('upstream', 'downstream'):
            logger.debug(f"拓扑权重: {relationship} 关系 ({current_service} <-> {candidate_service}), +0.3")
            return 0.3
        elif relationship == 'sibling':
            logger.debug(f"拓扑权重: 兄弟服务 ({current_service} <-> {candidate_service}), +0.15")
            return 0.15
        
        return 0.0
        
    except Exception as e:
        logger.warning(f"计算拓扑权重失败: {e}")
        return 0.0


def analyze_temporal_causality(
    alert_a: AlertContext,
    alert_b: AlertContext,
    topology_manager: Any = None
) -> float:
    """
    分析两个告警之间的时间因果性
    
    规则：
    - 如果 A 发生在 B 之前（时间差 < 5分钟）且 A 的服务是 B 的上游 → A 可能是根因
    - 返回因果性得分 0-1
    
    Args:
        alert_a: 告警 A（潜在根因）
        alert_b: 告警 B（当前告警）
        topology_manager: 拓扑管理器实例
        
    Returns:
        float: 因果性得分 (0.0 - 1.0)
    """
    # A 必须在 B 之前发生
    if not alert_a.timestamp or not alert_b.timestamp:
        return 0.0
    
    time_delta = (alert_b.timestamp - alert_a.timestamp).total_seconds()
    
    # A 必须在 B 之前
    if time_delta <= 0:
        return 0.0
    
    # 时间窗口: 5 分钟内
    max_window = 300  # 秒
    if time_delta > max_window:
        return 0.0
    
    # 基础时间因果性得分（越接近越高）
    time_score = 1.0 - (time_delta / max_window)
    
    # 如果有拓扑信息，增强因果性判断
    if topology_manager:
        try:
            service_a = _get_service_from_alert(alert_a)
            service_b = _get_service_from_alert(alert_b)
            
            if service_a and service_b and service_a != service_b:
                # 检查 A 是否是 B 的上游
                b_upstream = topology_manager.get_upstream(service_b)
                if service_a in b_upstream:
                    # A 是 B 的上游，增强因果性得分
                    time_score = min(1.0, time_score * 1.5)
                    logger.debug(f"时间因果性: {service_a} 是 {service_b} 的上游，增强得分至 {time_score:.2f}")
        except Exception as e:
            logger.warning(f"时间因果性拓扑检查失败: {e}")
    
    return round(time_score, 4)


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


def infer_propagation_path(
    alerts: List[AlertContext],
    topology_manager: Any = None
) -> List[dict]:
    """
    推理告警传播路径
    
    沿拓扑图推理可能的故障传播路径，返回最可能的根因告警
    
    Args:
        alerts: 近期告警列表（按时间排序）
        topology_manager: 拓扑管理器实例
        
    Returns:
        List[dict]: 推理结果，包含可能的传播路径和根因候选
    """
    if not alerts or len(alerts) < 2:
        return []
    
    if not topology_manager:
        topology_manager = _get_topology_manager()
    
    if not topology_manager:
        return []
    
    try:
        # 提取所有告警的服务
        service_alerts: dict[str, List[AlertContext]] = {}
        for alert in alerts:
            service = _get_service_from_alert(alert)
            if service and service != 'unknown':
                if service not in service_alerts:
                    service_alerts[service] = []
                service_alerts[service].append(alert)
        
        if len(service_alerts) < 2:
            return []
        
        # 分析传播路径
        paths = []
        services = list(service_alerts.keys())
        
        for i, service_a in enumerate(services):
            alerts_a = service_alerts[service_a]
            earliest_a = min(a.timestamp for a in alerts_a if a.timestamp)
            
            for service_b in services[i+1:]:
                # 检查是否有上下游关系
                is_related, relationship = topology_manager.are_related(service_a, service_b)
                
                if not is_related or relationship not in ('upstream', 'downstream'):
                    continue
                
                alerts_b = service_alerts[service_b]
                earliest_b = min(b.timestamp for b in alerts_b if b.timestamp)
                
                time_delta = (earliest_b - earliest_a).total_seconds()
                
                # 确定根因和影响方向
                if relationship == 'upstream':
                    # A 是 B 的上游
                    if time_delta > 0:
                        # A 先于 B 发生，A 可能是根因
                        root_cause = service_a
                        affected = service_b
                        confidence = min(1.0, 0.7 + 0.3 * (1 - abs(time_delta) / 300))
                    else:
                        root_cause = service_b
                        affected = service_a
                        confidence = 0.4
                elif relationship == 'downstream':
                    # A 是 B 的下游
                    if time_delta < 0:
                        # B 先于 A 发生，B 可能是根因
                        root_cause = service_b
                        affected = service_a
                        confidence = min(1.0, 0.7 + 0.3 * (1 - abs(time_delta) / 300))
                    else:
                        root_cause = service_a
                        affected = service_b
                        confidence = 0.4
                else:
                    continue
                
                paths.append({
                    'root_cause_service': root_cause,
                    'affected_service': affected,
                    'time_delta': round(time_delta, 2),
                    'confidence': round(confidence, 2),
                    'root_cause_alerts': [a.event_id for a in service_alerts[root_cause] if a.event_id],
                    'affected_alerts': [a.event_id for a in service_alerts[affected] if a.event_id]
                })
        
        # 按置信度排序
        paths.sort(key=lambda x: x['confidence'], reverse=True)
        
        logger.debug(f"推理传播路径: 发现 {len(paths)} 条可能路径")
        return paths
        
    except Exception as e:
        logger.warning(f"推理传播路径失败: {e}")
        return []


def score_candidate_enhanced(
    current: AlertContext,
    candidate: AlertContext,
    window_minutes: int,
    topology_manager: Any = None,
    session: Any = None
) -> float:
    """
    增强版候选告警评分函数
    
    在原有评分基础上增加：
    - 拓扑权重
    - 历史频率权重
    - 时间因果性因子
    
    Args:
        current: 当前告警上下文
        candidate: 候选告警上下文
        window_minutes: 时间窗口（分钟）
        topology_manager: 拓扑管理器实例
        session: 数据库会话
        
    Returns:
        float: 综合评分 (0.0 - 1.0)
    """
    # 获取基础评分
    base_score = score_candidate(current, candidate, window_minutes)
    
    if base_score <= 0:
        return 0.0
    
    # 计算拓扑权重
    topo_weight = 0.0
    if topology_manager is None:
        topology_manager = _get_topology_manager()
    
    if topology_manager:
        topo_weight = calculate_topology_weight(current, candidate, topology_manager)
    
    # 计算历史权重
    hist_weight = 0.0
    if current.alert_hash and candidate.alert_hash:
        hist_weight = get_historical_weight(
            current.alert_hash, candidate.alert_hash, session
        )
    
    # 计算时间因果性因子
    causality_factor = 1.0
    if candidate.timestamp and current.timestamp:
        causality_score = analyze_temporal_causality(
            candidate, current, topology_manager
        )
        if causality_score > 0.5:
            causality_factor = 1.0 + 0.2 * causality_score
    
    # 综合评分
    # 基础分 * 因果因子 + 拓扑权重 + 历史权重
    enhanced_score = base_score * causality_factor + topo_weight + hist_weight
    
    # 归一化到 0-1
    final_score = min(1.0, max(0.0, enhanced_score))
    
    logger.debug(f"增强评分: base={base_score:.3f}, topo={topo_weight:.3f}, "
                f"hist={hist_weight:.3f}, causality={causality_factor:.3f}, "
                f"final={final_score:.3f}")
    
    # 更新关联记录
    if current.alert_hash and candidate.alert_hash and final_score > 0.3:
        time_delta = 0.0
        if candidate.timestamp and current.timestamp:
            time_delta = (current.timestamp - candidate.timestamp).total_seconds()
        update_alert_correlation(
            current.alert_hash,
            candidate.alert_hash,
            time_delta,
            final_score,
            session
        )
    
    return round(final_score, 4)
