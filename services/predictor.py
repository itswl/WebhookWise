"""预测引擎 - 基于统计方法的告警趋势预测和异常检测"""

import logging
import math
import json
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)


class AlertPredictor:
    """告警预测引擎 - 使用轻量统计方法，不依赖 ML 框架"""
    
    def __init__(self):
        self._predictions = []
        self._last_run = None
    
    def detect_anomalies(self, session, window_hours: int = 1) -> List[dict]:
        """异常检测 - 基于移动平均 + 标准差
        
        比较最近 N 小时的告警频率与历史基线，检测异常。
        
        方法：
        - 计算过去7天同一时段（±1小时）的告警频率均值和标准差
        - 如果当前频率 > 均值 + 2*标准差，标记为异常
        
        Returns:
            [{"type": "frequency_anomaly", "source": "xxx",
              "current_rate": float, "baseline_rate": float, "std_dev": float,
              "deviation": float, "severity": "high|medium|low",
              "description": "xxx"}]
        """
        from core.models import WebhookEvent
        from sqlalchemy import func
        
        anomalies = []
        now = datetime.now()
        current_hour = now.hour
        
        try:
            # 获取所有告警来源
            sources = session.query(
                func.distinct(WebhookEvent.source)
            ).filter(
                WebhookEvent.source.isnot(None)
            ).all()
            sources = [s[0] for s in sources if s[0]]
            
            for source in sources:
                # 计算当前窗口的告警数
                window_start = now - timedelta(hours=window_hours)
                current_count = session.query(func.count(WebhookEvent.id)).filter(
                    WebhookEvent.source == source,
                    WebhookEvent.timestamp >= window_start
                ).scalar() or 0
                
                current_rate = current_count / max(window_hours, 1)
                
                # 计算历史基线（过去7天同一时段）
                historical_rates = []
                for days_ago in range(1, 8):
                    ref_time = now - timedelta(days=days_ago)
                    ref_start = ref_time - timedelta(hours=window_hours)
                    ref_end = ref_time
                    
                    hist_count = session.query(func.count(WebhookEvent.id)).filter(
                        WebhookEvent.source == source,
                        WebhookEvent.timestamp >= ref_start,
                        WebhookEvent.timestamp < ref_end
                    ).scalar() or 0
                    
                    historical_rates.append(hist_count / max(window_hours, 1))
                
                if not historical_rates:
                    continue
                
                # 计算均值和标准差
                baseline_rate = sum(historical_rates) / len(historical_rates)
                
                if len(historical_rates) > 1:
                    variance = sum((r - baseline_rate) ** 2 for r in historical_rates) / len(historical_rates)
                    std_dev = math.sqrt(variance)
                else:
                    std_dev = 0
                
                # 检测异常：当前频率超过 均值 + 2*标准差
                threshold = baseline_rate + 2 * std_dev
                
                if current_rate > threshold and current_rate > 0:
                    # 计算偏离程度
                    if std_dev > 0:
                        deviation = (current_rate - baseline_rate) / std_dev
                    else:
                        deviation = float('inf') if current_rate > baseline_rate else 0
                    
                    # 判定严重程度
                    if deviation >= 4 or current_rate >= baseline_rate * 5:
                        severity = 'high'
                    elif deviation >= 3 or current_rate >= baseline_rate * 3:
                        severity = 'medium'
                    else:
                        severity = 'low'
                    
                    anomalies.append({
                        'type': 'frequency_anomaly',
                        'source': source,
                        'current_rate': round(current_rate, 2),
                        'baseline_rate': round(baseline_rate, 2),
                        'std_dev': round(std_dev, 2),
                        'deviation': round(deviation, 2) if deviation != float('inf') else 999,
                        'threshold': round(threshold, 2),
                        'severity': severity,
                        'description': f'{source} 告警频率异常：当前 {current_rate:.1f}/h，基线 {baseline_rate:.1f}/h',
                        'detected_at': now.isoformat()
                    })
            
            logger.info(f"异常检测完成：发现 {len(anomalies)} 个异常")
            
        except Exception as e:
            logger.error(f"异常检测失败: {e}", exc_info=True)
        
        return anomalies
    
    def predict_alert_trend(self, session, forecast_hours: int = 6) -> List[dict]:
        """告警趋势预测 - 基于指数平滑法
        
        预测未来 N 小时的告警频率趋势。
        
        方法：
        - 收集过去24小时的每小时告警数
        - 使用简单指数平滑 (alpha=0.3) 预测未来趋势
        - 如果预测值持续上升，发出预警
        
        Returns:
            [{"type": "trend_prediction", "source": "xxx",
              "current_hourly_rate": float, "predicted_rate": float,
              "trend": "increasing|stable|decreasing",
              "confidence": float, "forecast_hours": int,
              "description": "xxx"}]
        """
        from core.models import WebhookEvent
        from sqlalchemy import func, extract
        
        predictions = []
        now = datetime.now()
        alpha = 0.3  # 指数平滑系数
        
        try:
            # 获取活跃的告警来源
            active_threshold = now - timedelta(days=7)
            sources = session.query(
                func.distinct(WebhookEvent.source)
            ).filter(
                WebhookEvent.source.isnot(None),
                WebhookEvent.timestamp >= active_threshold
            ).all()
            sources = [s[0] for s in sources if s[0]]
            
            for source in sources:
                # 收集过去24小时的每小时告警数
                hourly_counts = []
                for hours_ago in range(24, 0, -1):
                    hour_start = now - timedelta(hours=hours_ago)
                    hour_end = hour_start + timedelta(hours=1)
                    
                    count = session.query(func.count(WebhookEvent.id)).filter(
                        WebhookEvent.source == source,
                        WebhookEvent.timestamp >= hour_start,
                        WebhookEvent.timestamp < hour_end
                    ).scalar() or 0
                    
                    hourly_counts.append(count)
                
                if not hourly_counts or sum(hourly_counts) == 0:
                    continue
                
                # 指数平滑预测
                smoothed = hourly_counts[0]
                for count in hourly_counts[1:]:
                    smoothed = alpha * count + (1 - alpha) * smoothed
                
                # 预测未来值（简化：假设趋势延续）
                current_rate = hourly_counts[-1]
                predicted_rate = smoothed
                
                # 计算趋势：比较最近3小时和之前3小时的平均值
                recent_avg = sum(hourly_counts[-3:]) / 3 if len(hourly_counts) >= 3 else current_rate
                earlier_avg = sum(hourly_counts[-6:-3]) / 3 if len(hourly_counts) >= 6 else recent_avg
                
                if recent_avg > earlier_avg * 1.2:
                    trend = 'increasing'
                    trend_ratio = recent_avg / max(earlier_avg, 0.1)
                elif recent_avg < earlier_avg * 0.8:
                    trend = 'decreasing'
                    trend_ratio = earlier_avg / max(recent_avg, 0.1)
                else:
                    trend = 'stable'
                    trend_ratio = 1.0
                
                # 置信度基于数据一致性
                if len(hourly_counts) >= 6:
                    variance = sum((c - smoothed) ** 2 for c in hourly_counts[-6:]) / 6
                    confidence = max(0.3, 1 - min(1, math.sqrt(variance) / max(smoothed, 1)))
                else:
                    confidence = 0.5
                
                # 只记录有意义的预测（上升趋势或当前活跃）
                if trend == 'increasing' or current_rate >= 5:
                    predictions.append({
                        'type': 'trend_prediction',
                        'source': source,
                        'current_hourly_rate': round(current_rate, 2),
                        'predicted_rate': round(predicted_rate, 2),
                        'trend': trend,
                        'trend_ratio': round(trend_ratio, 2),
                        'confidence': round(confidence, 2),
                        'forecast_hours': forecast_hours,
                        'description': f'{source} 趋势{_trend_desc(trend)}，当前 {current_rate:.0f}/h',
                        'predicted_at': now.isoformat()
                    })
            
            logger.info(f"趋势预测完成：生成 {len(predictions)} 条预测")
            
        except Exception as e:
            logger.error(f"趋势预测失败: {e}", exc_info=True)
        
        return predictions
    
    def predict_alert_storm(self, session, threshold_multiplier: float = 3.0) -> List[dict]:
        """告警风暴预警
        
        检测告警频率是否在加速上升，预测是否即将发生告警风暴。
        
        方法：
        - 计算最近3个时间窗口（各10分钟）的告警增长率
        - 如果增长率连续为正且加速 → 预测风暴
        
        Returns:
            [{"type": "storm_prediction", "estimated_time_minutes": int,
              "current_rate": float, "acceleration": float,
              "confidence": float, "affected_sources": [...],
              "description": "xxx"}]
        """
        from core.models import WebhookEvent
        from sqlalchemy import func
        
        warnings = []
        now = datetime.now()
        window_minutes = 10
        
        try:
            # 计算最近3个窗口的告警数
            windows = []
            for i in range(3):
                window_end = now - timedelta(minutes=i * window_minutes)
                window_start = window_end - timedelta(minutes=window_minutes)
                
                count = session.query(func.count(WebhookEvent.id)).filter(
                    WebhookEvent.timestamp >= window_start,
                    WebhookEvent.timestamp < window_end
                ).scalar() or 0
                
                windows.append({
                    'start': window_start,
                    'end': window_end,
                    'count': count
                })
            
            # windows[0] 是最近的，windows[2] 是最早的
            windows.reverse()  # 时间顺序排列
            
            if len(windows) < 3:
                return warnings
            
            counts = [w['count'] for w in windows]
            
            # 计算增长率（每窗口）
            growth_rates = []
            for i in range(1, len(counts)):
                if counts[i-1] > 0:
                    rate = (counts[i] - counts[i-1]) / counts[i-1]
                else:
                    rate = counts[i] if counts[i] > 0 else 0
                growth_rates.append(rate)
            
            # 计算加速度（增长率的变化）
            if len(growth_rates) >= 2:
                acceleration = growth_rates[-1] - growth_rates[0]
            else:
                acceleration = 0
            
            current_rate = counts[-1] / window_minutes  # 每分钟告警数
            
            # 风暴检测条件：
            # 1. 最近窗口告警数超过阈值
            # 2. 连续增长
            # 3. 加速度为正
            baseline_rate = 2  # 基线：每10分钟2个告警
            
            is_storm_coming = (
                counts[-1] >= baseline_rate * threshold_multiplier and
                all(r > 0 for r in growth_rates) and
                acceleration > 0
            )
            
            if is_storm_coming:
                # 估算风暴到达时间
                if acceleration > 0:
                    # 简化估算：基于当前加速度
                    estimated_minutes = max(5, int(30 / (1 + acceleration)))
                else:
                    estimated_minutes = 30
                
                # 置信度基于增长一致性
                confidence = min(0.9, 0.5 + 0.2 * len([r for r in growth_rates if r > 0]))
                
                # 获取受影响的来源
                affected_start = now - timedelta(minutes=window_minutes)
                affected_sources = session.query(
                    func.distinct(WebhookEvent.source)
                ).filter(
                    WebhookEvent.timestamp >= affected_start,
                    WebhookEvent.source.isnot(None)
                ).limit(10).all()
                affected_sources = [s[0] for s in affected_sources if s[0]]
                
                warnings.append({
                    'type': 'storm_prediction',
                    'estimated_time_minutes': estimated_minutes,
                    'current_rate': round(current_rate, 2),
                    'acceleration': round(acceleration, 3),
                    'confidence': round(confidence, 2),
                    'affected_sources': affected_sources,
                    'recent_counts': counts,
                    'growth_rates': [round(r, 2) for r in growth_rates],
                    'description': f'预测告警风暴，约 {estimated_minutes} 分钟后可能爆发，当前 {current_rate:.1f}/min',
                    'predicted_at': now.isoformat()
                })
            
            logger.info(f"风暴预警检测完成：{len(warnings)} 条预警")
            
        except Exception as e:
            logger.error(f"风暴预警检测失败: {e}", exc_info=True)
        
        return warnings
    
    def get_all_predictions(self, session) -> dict:
        """获取所有预测结果"""
        anomalies = self.detect_anomalies(session)
        trends = self.predict_alert_trend(session)
        storm_warnings = self.predict_alert_storm(session)
        
        return {
            'anomalies': anomalies,
            'anomalies_count': len(anomalies),
            'trends': trends,
            'trends_count': len(trends),
            'storm_warnings': storm_warnings,
            'storm_warnings_count': len(storm_warnings),
            'generated_at': datetime.utcnow().isoformat()
        }
    
    def run_prediction_cycle(self, session) -> dict:
        """执行一次完整的预测周期（由后台调度器调用）"""
        logger.info("Running prediction cycle...")
        predictions = self.get_all_predictions(session)
        
        # 保存预测结果到数据库
        self._save_predictions(session, predictions)
        self._last_run = datetime.utcnow()
        
        logger.info(f"Prediction cycle complete: {predictions.get('anomalies_count', 0)} anomalies, "
                    f"{predictions.get('trends_count', 0)} trends, "
                    f"{predictions.get('storm_warnings_count', 0)} storm warnings")
        
        return predictions
    
    def _save_predictions(self, session, predictions: dict) -> None:
        """保存预测结果到 Prediction 表"""
        from core.models import Prediction
        from sqlalchemy import func
        
        try:
            now = datetime.utcnow()
            expires_at = now + timedelta(hours=1)  # 预测结果1小时后过期
            
            # 保存异常检测结果
            for anomaly in predictions.get('anomalies', []):
                pred = Prediction(
                    prediction_type='anomaly',
                    target=anomaly.get('source'),
                    predicted_value=anomaly.get('current_rate'),
                    confidence=1 - (anomaly.get('severity') == 'low') * 0.3,
                    details=json.dumps(anomaly, ensure_ascii=False),
                    expires_at=expires_at
                )
                session.add(pred)
            
            # 保存趋势预测结果
            for trend in predictions.get('trends', []):
                pred = Prediction(
                    prediction_type='trend',
                    target=trend.get('source'),
                    predicted_value=trend.get('predicted_rate'),
                    confidence=trend.get('confidence', 0.5),
                    details=json.dumps(trend, ensure_ascii=False),
                    expires_at=expires_at
                )
                session.add(pred)
            
            # 保存风暴预警
            for storm in predictions.get('storm_warnings', []):
                pred = Prediction(
                    prediction_type='storm',
                    target=','.join(storm.get('affected_sources', [])[:5]),
                    predicted_value=storm.get('current_rate'),
                    confidence=storm.get('confidence', 0.5),
                    details=json.dumps(storm, ensure_ascii=False),
                    expires_at=expires_at
                )
                session.add(pred)
            
            session.commit()
            logger.debug(f"保存了 {len(predictions.get('anomalies', []))} 条异常、"
                        f"{len(predictions.get('trends', []))} 条趋势、"
                        f"{len(predictions.get('storm_warnings', []))} 条风暴预警")
            
        except Exception as e:
            logger.error(f"保存预测结果失败: {e}", exc_info=True)
            session.rollback()


def _trend_desc(trend: str) -> str:
    """趋势描述"""
    mapping = {
        'increasing': '上升',
        'decreasing': '下降',
        'stable': '平稳'
    }
    return mapping.get(trend, trend)


# 全局单例
alert_predictor = AlertPredictor()
