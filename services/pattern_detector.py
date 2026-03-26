"""历史告警模式检测器 - 从历史数据中发现周期性规律和关联模式"""

import logging
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from typing import Dict, List, Tuple, Optional, Any

logger = logging.getLogger(__name__)


class PatternDetector:
    """告警模式检测器"""
    
    def __init__(self):
        self._patterns = []  # 已发现的模式缓存
        self._last_analysis = None
    
    def detect_periodic_patterns(self, session, lookback_days: int = 30) -> List[dict]:
        """检测周期性告警模式
        
        分析历史数据，找出周期性出现的告警：
        - 每天固定时段出现（如每天凌晨2点的备份告警）
        - 每周固定时间（如每周一早高峰 CPU 告警）
        - 周期性重复（每隔 N 小时出现一次）
        
        Returns:
            [{"pattern_type": "daily|weekly|periodic", "alert_hash": "xxx", 
              "source": "xxx", "period_hours": N, "confidence": 0.0-1.0,
              "typical_time": "HH:MM", "description": "xxx"}]
        """
        from core.models import WebhookEvent
        from sqlalchemy import func
        
        patterns = []
        time_threshold = datetime.now() - timedelta(days=lookback_days)
        
        try:
            # 1. 按 alert_hash 分组统计
            hash_stats = session.query(
                WebhookEvent.alert_hash,
                WebhookEvent.source,
                func.count(WebhookEvent.id).label('total_count'),
                func.min(WebhookEvent.timestamp).label('first_seen'),
                func.max(WebhookEvent.timestamp).label('last_seen')
            ).filter(
                WebhookEvent.timestamp >= time_threshold,
                WebhookEvent.alert_hash.isnot(None),
                WebhookEvent.is_duplicate == 0  # 只看原始告警
            ).group_by(
                WebhookEvent.alert_hash,
                WebhookEvent.source
            ).having(
                func.count(WebhookEvent.id) >= 3  # 至少出现3次才有周期意义
            ).all()
            
            for stat in hash_stats:
                if not stat.alert_hash:
                    continue
                    
                # 查询该 hash 的所有时间戳
                timestamps = session.query(WebhookEvent.timestamp).filter(
                    WebhookEvent.alert_hash == stat.alert_hash,
                    WebhookEvent.timestamp >= time_threshold,
                    WebhookEvent.is_duplicate == 0
                ).order_by(WebhookEvent.timestamp.asc()).all()
                
                timestamps = [t[0] for t in timestamps if t[0]]
                
                if len(timestamps) < 3:
                    continue
                
                # 分析周期性
                pattern = self._analyze_periodicity(
                    timestamps,
                    stat.alert_hash,
                    stat.source,
                    stat.total_count
                )
                
                if pattern and pattern.get('confidence', 0) >= 0.5:
                    patterns.append(pattern)
                    
            logger.info(f"检测到 {len(patterns)} 个周期性模式")
            
        except Exception as e:
            logger.error(f"检测周期性模式失败: {e}", exc_info=True)
        
        return patterns
    
    def _analyze_periodicity(
        self,
        timestamps: List[datetime],
        alert_hash: str,
        source: str,
        total_count: int
    ) -> Optional[dict]:
        """分析时间序列的周期性"""
        if len(timestamps) < 3:
            return None
        
        # 计算相邻时间差（小时）
        deltas_hours = []
        for i in range(1, len(timestamps)):
            delta = (timestamps[i] - timestamps[i-1]).total_seconds() / 3600
            if delta > 0:
                deltas_hours.append(delta)
        
        if not deltas_hours:
            return None
        
        # 检测每日模式（检查小时分布）
        hour_counter = Counter(t.hour for t in timestamps)
        most_common_hour, hour_count = hour_counter.most_common(1)[0]
        hour_concentration = hour_count / len(timestamps)
        
        # 检测每周模式（检查星期分布）
        weekday_counter = Counter(t.weekday() for t in timestamps)
        most_common_weekday, weekday_count = weekday_counter.most_common(1)[0]
        weekday_concentration = weekday_count / len(timestamps)
        
        # 检测固定周期（频率分析）
        avg_delta = sum(deltas_hours) / len(deltas_hours)
        delta_variance = sum((d - avg_delta) ** 2 for d in deltas_hours) / len(deltas_hours)
        delta_std = delta_variance ** 0.5
        
        # 周期性置信度计算
        periodic_confidence = 0.0
        if avg_delta > 0:
            # 标准差越小，周期越稳定
            cv = delta_std / avg_delta if avg_delta > 0 else 1.0  # 变异系数
            periodic_confidence = max(0, 1 - cv)
        
        # 判断模式类型
        pattern_type = None
        period_hours = None
        typical_time = None
        confidence = 0.0
        description = None
        
        # 优先检测：每日固定时段
        if hour_concentration >= 0.6:
            pattern_type = 'daily'
            period_hours = 24
            typical_time = f"{most_common_hour:02d}:00"
            confidence = hour_concentration
            description = f"每天 {typical_time} 左右出现，历史 {total_count} 次"
        
        # 每周固定时间
        elif weekday_concentration >= 0.5 and total_count >= 4:
            weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
            pattern_type = 'weekly'
            period_hours = 168
            typical_time = f"{weekday_names[most_common_weekday]} {most_common_hour:02d}:00"
            confidence = weekday_concentration * 0.8 + hour_concentration * 0.2
            description = f"每{typical_time}左右出现，历史 {total_count} 次"
        
        # 固定周期
        elif periodic_confidence >= 0.5 and 1 <= avg_delta <= 168:
            pattern_type = 'periodic'
            period_hours = round(avg_delta, 1)
            typical_time = None
            confidence = periodic_confidence
            if period_hours < 1:
                description = f"约每 {int(period_hours * 60)} 分钟出现一次"
            elif period_hours < 24:
                description = f"约每 {period_hours:.1f} 小时出现一次"
            else:
                description = f"约每 {period_hours / 24:.1f} 天出现一次"
        
        if not pattern_type:
            return None
        
        return {
            'pattern_type': pattern_type,
            'alert_hash': alert_hash[:16] + '...',
            'alert_hash_full': alert_hash,
            'source': source,
            'period_hours': period_hours,
            'confidence': round(confidence, 3),
            'typical_time': typical_time,
            'description': description,
            'occurrence_count': total_count
        }
    
    def detect_burst_patterns(self, session, window_minutes: int = 30) -> List[dict]:
        """检测告警突发/风暴模式
        
        识别短时间内告警数量激增的情况
        
        Returns:
            [{"burst_start": datetime, "burst_end": datetime, "alert_count": N,
              "normal_rate": float, "burst_rate": float, "sources": [...],
              "severity": "high|medium|low"}]
        """
        from core.models import WebhookEvent
        from sqlalchemy import func
        
        bursts = []
        now = datetime.now()
        
        try:
            # 1. 计算过去7天的正常基线（平均每窗口告警数）
            baseline_start = now - timedelta(days=7)
            baseline_end = now - timedelta(hours=1)  # 排除最近1小时
            
            baseline_count = session.query(func.count(WebhookEvent.id)).filter(
                WebhookEvent.timestamp >= baseline_start,
                WebhookEvent.timestamp < baseline_end
            ).scalar() or 0
            
            # 计算窗口数量
            baseline_hours = (baseline_end - baseline_start).total_seconds() / 3600
            windows_in_baseline = max(1, baseline_hours * 60 / window_minutes)
            normal_rate = baseline_count / windows_in_baseline
            
            # 2. 检查最近24小时的每个窗口
            check_start = now - timedelta(hours=24)
            
            # 按窗口统计告警
            window_seconds = window_minutes * 60
            current_window_start = check_start
            
            while current_window_start < now:
                window_end = current_window_start + timedelta(minutes=window_minutes)
                
                # 查询该窗口的告警
                window_stats = session.query(
                    func.count(WebhookEvent.id).label('count'),
                    func.array_agg(func.distinct(WebhookEvent.source)).label('sources')
                ).filter(
                    WebhookEvent.timestamp >= current_window_start,
                    WebhookEvent.timestamp < window_end
                ).first()
                
                window_count = window_stats.count if window_stats else 0
                
                # 突发检测：超过基线3倍
                burst_threshold = max(normal_rate * 3, 10)  # 至少10个才算突发
                
                if window_count >= burst_threshold:
                    # 计算严重程度
                    ratio = window_count / max(normal_rate, 1)
                    if ratio >= 10:
                        severity = 'high'
                    elif ratio >= 5:
                        severity = 'medium'
                    else:
                        severity = 'low'
                    
                    # 获取该窗口的来源列表
                    sources_query = session.query(
                        func.distinct(WebhookEvent.source)
                    ).filter(
                        WebhookEvent.timestamp >= current_window_start,
                        WebhookEvent.timestamp < window_end
                    ).all()
                    sources = [s[0] for s in sources_query if s[0]]
                    
                    bursts.append({
                        'burst_start': current_window_start.isoformat(),
                        'burst_end': window_end.isoformat(),
                        'alert_count': window_count,
                        'normal_rate': round(normal_rate, 2),
                        'burst_rate': round(window_count, 2),
                        'ratio': round(ratio, 2),
                        'sources': sources[:10],  # 最多显示10个来源
                        'severity': severity
                    })
                
                current_window_start = window_end
            
            logger.info(f"检测到 {len(bursts)} 个告警突发模式")
            
        except Exception as e:
            logger.error(f"检测告警突发模式失败: {e}", exc_info=True)
        
        return bursts
    
    def detect_correlation_rules(self, session, lookback_days: int = 14) -> List[dict]:
        """挖掘告警关联规则
        
        发现 "A 告警后 X 分钟内经常跟着 B 告警" 的模式
        
        Returns:
            [{"alert_a_hash": "xxx", "alert_b_hash": "xxx", 
              "avg_delay_seconds": float, "co_occurrence_count": int,
              "confidence": float, "description": "xxx"}]
        """
        from core.models import WebhookEvent, AlertCorrelation
        from sqlalchemy import func
        
        rules = []
        
        try:
            # 从 AlertCorrelation 表读取已有的关联规则
            # 这些规则是由 alert_noise_reduction 模块实时更新的
            correlations = session.query(AlertCorrelation).filter(
                AlertCorrelation.co_occurrence_count >= 3,  # 至少共现3次
                AlertCorrelation.confidence >= 0.3
            ).order_by(
                AlertCorrelation.confidence.desc()
            ).limit(100).all()
            
            for corr in correlations:
                # 获取告警来源信息
                source_a = self._get_source_for_hash(session, corr.alert_hash_a)
                source_b = self._get_source_for_hash(session, corr.alert_hash_b)
                
                # 生成描述
                if corr.avg_time_delta > 0:
                    time_desc = f"A 在 B 之前约 {abs(corr.avg_time_delta):.0f} 秒"
                elif corr.avg_time_delta < 0:
                    time_desc = f"B 在 A 之前约 {abs(corr.avg_time_delta):.0f} 秒"
                else:
                    time_desc = "A 和 B 几乎同时发生"
                
                rules.append({
                    'alert_a_hash': corr.alert_hash_a[:16] + '...',
                    'alert_a_hash_full': corr.alert_hash_a,
                    'alert_a_source': source_a,
                    'alert_b_hash': corr.alert_hash_b[:16] + '...',
                    'alert_b_hash_full': corr.alert_hash_b,
                    'alert_b_source': source_b,
                    'avg_delay_seconds': round(corr.avg_time_delta, 2),
                    'co_occurrence_count': corr.co_occurrence_count,
                    'confidence': round(corr.confidence, 3),
                    'last_seen': corr.last_seen.isoformat() if corr.last_seen else None,
                    'description': f"共现 {corr.co_occurrence_count} 次，{time_desc}"
                })
            
            logger.info(f"获取到 {len(rules)} 条告警关联规则")
            
        except Exception as e:
            logger.error(f"获取告警关联规则失败: {e}", exc_info=True)
        
        return rules
    
    def _get_source_for_hash(self, session, alert_hash: str) -> Optional[str]:
        """根据告警哈希获取来源"""
        from core.models import WebhookEvent
        
        try:
            event = session.query(WebhookEvent.source).filter(
                WebhookEvent.alert_hash == alert_hash
            ).first()
            return event[0] if event else None
        except Exception:
            return None
    
    def get_all_patterns(self, session) -> dict:
        """获取所有检测到的模式（整合结果）"""
        try:
            periodic = self.detect_periodic_patterns(session)
            bursts = self.detect_burst_patterns(session)
            correlations = self.detect_correlation_rules(session)
            
            self._patterns = {
                'periodic': periodic,
                'bursts': bursts,
                'correlations': correlations
            }
            self._last_analysis = datetime.utcnow()
            
            return {
                'periodic': periodic,
                'periodic_count': len(periodic),
                'bursts': bursts,
                'bursts_count': len(bursts),
                'correlations': correlations,
                'correlations_count': len(correlations),
                'analyzed_at': self._last_analysis.isoformat()
            }
            
        except Exception as e:
            logger.error(f"获取所有模式失败: {e}", exc_info=True)
            return {
                'periodic': [],
                'bursts': [],
                'correlations': [],
                'error': str(e),
                'analyzed_at': datetime.utcnow().isoformat()
            }


# 全局单例
pattern_detector = PatternDetector()
