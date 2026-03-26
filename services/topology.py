"""
服务拓扑管理器

维护服务间的 DAG 依赖关系，支持：
- 手动添加/移除依赖关系
- 从数据库加载拓扑
- 从历史告警数据自动发现潜在依赖关系
"""

import logging
from typing import Dict, List, Set, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class ServiceTopology:
    """服务依赖拓扑管理器 - 维护服务间的 DAG 依赖关系"""
    
    def __init__(self):
        self._graph: Dict[str, Set[str]] = defaultdict(set)  # service -> depends_on
        self._reverse_graph: Dict[str, Set[str]] = defaultdict(set)  # service -> depended_by
        self._loaded = False
    
    def add_dependency(self, service: str, depends_on: str) -> bool:
        """
        添加服务依赖关系
        
        Args:
            service: 服务名称
            depends_on: 该服务依赖的服务名称
            
        Returns:
            bool: 是否成功添加（如果已存在则返回 False）
        """
        if not service or not depends_on:
            logger.warning("添加依赖失败: 服务名称不能为空")
            return False
        
        service = service.strip().lower()
        depends_on = depends_on.strip().lower()
        
        if service == depends_on:
            logger.warning(f"添加依赖失败: 服务不能依赖自身 ({service})")
            return False
        
        # 检查是否会形成环
        if self._would_create_cycle(service, depends_on):
            logger.warning(f"添加依赖失败: 会形成循环依赖 ({service} -> {depends_on})")
            return False
        
        if depends_on in self._graph[service]:
            logger.debug(f"依赖关系已存在: {service} -> {depends_on}")
            return False
        
        self._graph[service].add(depends_on)
        self._reverse_graph[depends_on].add(service)
        logger.info(f"添加服务依赖: {service} -> {depends_on}")
        return True
    
    def _would_create_cycle(self, service: str, depends_on: str) -> bool:
        """检查添加依赖是否会形成环"""
        # 如果 depends_on 的上游中包含 service，则会形成环
        visited: Set[str] = set()
        stack = [depends_on]
        
        while stack:
            current = stack.pop()
            if current == service:
                return True
            if current in visited:
                continue
            visited.add(current)
            stack.extend(self._graph.get(current, set()))
        
        return False
    
    def remove_dependency(self, service: str, depends_on: str) -> bool:
        """
        移除服务依赖关系
        
        Args:
            service: 服务名称
            depends_on: 该服务依赖的服务名称
            
        Returns:
            bool: 是否成功移除
        """
        service = service.strip().lower()
        depends_on = depends_on.strip().lower()
        
        if depends_on not in self._graph.get(service, set()):
            logger.debug(f"依赖关系不存在: {service} -> {depends_on}")
            return False
        
        self._graph[service].discard(depends_on)
        self._reverse_graph[depends_on].discard(service)
        
        # 清理空集合
        if not self._graph[service]:
            del self._graph[service]
        if not self._reverse_graph[depends_on]:
            del self._reverse_graph[depends_on]
        
        logger.info(f"移除服务依赖: {service} -> {depends_on}")
        return True
    
    def get_upstream(self, service: str, depth: int = -1) -> Set[str]:
        """
        获取上游依赖（该服务依赖的服务）
        
        Args:
            service: 服务名称
            depth: 遍历深度，-1 表示无限深度
            
        Returns:
            Set[str]: 上游服务集合
        """
        service = service.strip().lower()
        result: Set[str] = set()
        visited: Set[str] = set()
        current_level = [service]
        current_depth = 0
        
        while current_level and (depth == -1 or current_depth < depth):
            next_level = []
            for svc in current_level:
                if svc in visited:
                    continue
                visited.add(svc)
                dependencies = self._graph.get(svc, set())
                for dep in dependencies:
                    if dep != service:  # 排除自身
                        result.add(dep)
                        next_level.append(dep)
            current_level = next_level
            current_depth += 1
        
        logger.debug(f"服务 {service} 的上游依赖 (depth={depth}): {result}")
        return result
    
    def get_downstream(self, service: str, depth: int = -1) -> Set[str]:
        """
        获取下游依赖（依赖该服务的服务）
        
        Args:
            service: 服务名称
            depth: 遍历深度，-1 表示无限深度
            
        Returns:
            Set[str]: 下游服务集合
        """
        service = service.strip().lower()
        result: Set[str] = set()
        visited: Set[str] = set()
        current_level = [service]
        current_depth = 0
        
        while current_level and (depth == -1 or current_depth < depth):
            next_level = []
            for svc in current_level:
                if svc in visited:
                    continue
                visited.add(svc)
                dependents = self._reverse_graph.get(svc, set())
                for dep in dependents:
                    if dep != service:  # 排除自身
                        result.add(dep)
                        next_level.append(dep)
            current_level = next_level
            current_depth += 1
        
        logger.debug(f"服务 {service} 的下游依赖 (depth={depth}): {result}")
        return result
    
    def are_related(self, service_a: str, service_b: str) -> Tuple[bool, str]:
        """
        判断两个服务是否有依赖关系
        
        Args:
            service_a: 服务 A
            service_b: 服务 B
            
        Returns:
            tuple: (is_related: bool, relationship: str)
                relationship 可能值: 'upstream', 'downstream', 'sibling', 'unrelated'
        """
        service_a = service_a.strip().lower()
        service_b = service_b.strip().lower()
        
        if service_a == service_b:
            return True, 'same'
        
        # 检查 A 是否是 B 的上游（B 依赖 A）
        b_upstream = self.get_upstream(service_b)
        if service_a in b_upstream:
            return True, 'upstream'
        
        # 检查 A 是否是 B 的下游（A 依赖 B）
        a_upstream = self.get_upstream(service_a)
        if service_b in a_upstream:
            return True, 'downstream'
        
        # 检查是否有共同上游（兄弟关系）
        common_upstream = a_upstream & b_upstream
        if common_upstream:
            return True, 'sibling'
        
        return False, 'unrelated'
    
    def get_topology_dict(self) -> dict:
        """
        导出拓扑为字典格式
        
        Returns:
            dict: 拓扑信息字典
        """
        return {
            'services': list(set(self._graph.keys()) | set(self._reverse_graph.keys())),
            'dependencies': {
                service: list(deps) for service, deps in self._graph.items()
            },
            'reverse_dependencies': {
                service: list(deps) for service, deps in self._reverse_graph.items()
            }
        }
    
    def load_from_db(self, session) -> int:
        """
        从数据库加载拓扑
        
        Args:
            session: SQLAlchemy session
            
        Returns:
            int: 加载的依赖关系数量
        """
        try:
            from core.models import ServiceTopologyModel
            
            records = session.query(ServiceTopologyModel).all()
            count = 0
            
            for record in records:
                if self.add_dependency(record.service_name, record.depends_on):
                    count += 1
            
            self._loaded = True
            logger.info(f"从数据库加载了 {count} 条服务依赖关系")
            return count
            
        except Exception as e:
            logger.error(f"从数据库加载拓扑失败: {e}")
            return 0
    
    def save_to_db(self, session, service: str, depends_on: str) -> bool:
        """
        保存单条依赖关系到数据库
        
        Args:
            session: SQLAlchemy session
            service: 服务名称
            depends_on: 依赖的服务名称
            
        Returns:
            bool: 是否保存成功
        """
        try:
            from core.models import ServiceTopologyModel
            
            service = service.strip().lower()
            depends_on = depends_on.strip().lower()
            
            # 检查是否已存在
            existing = session.query(ServiceTopologyModel).filter(
                ServiceTopologyModel.service_name == service,
                ServiceTopologyModel.depends_on == depends_on
            ).first()
            
            if existing:
                logger.debug(f"依赖关系已存在于数据库: {service} -> {depends_on}")
                return False
            
            record = ServiceTopologyModel(
                service_name=service,
                depends_on=depends_on
            )
            session.add(record)
            session.commit()
            logger.info(f"保存服务依赖到数据库: {service} -> {depends_on}")
            return True
            
        except Exception as e:
            logger.error(f"保存服务依赖到数据库失败: {e}")
            session.rollback()
            return False
    
    def delete_from_db(self, session, service: str, depends_on: str) -> bool:
        """
        从数据库删除依赖关系
        
        Args:
            session: SQLAlchemy session
            service: 服务名称
            depends_on: 依赖的服务名称
            
        Returns:
            bool: 是否删除成功
        """
        try:
            from core.models import ServiceTopologyModel
            
            service = service.strip().lower()
            depends_on = depends_on.strip().lower()
            
            deleted = session.query(ServiceTopologyModel).filter(
                ServiceTopologyModel.service_name == service,
                ServiceTopologyModel.depends_on == depends_on
            ).delete()
            
            session.commit()
            if deleted > 0:
                logger.info(f"从数据库删除服务依赖: {service} -> {depends_on}")
                return True
            return False
            
        except Exception as e:
            logger.error(f"从数据库删除服务依赖失败: {e}")
            session.rollback()
            return False
    
    def auto_discover_from_alerts(self, session, lookback_hours: int = 168) -> List[dict]:
        """
        从历史告警数据中自动发现潜在的服务依赖关系
        
        逻辑：如果服务 A 的告警经常在服务 B 告警之前出现，则 A 可能是 B 的上游
        
        Args:
            session: SQLAlchemy session
            lookback_hours: 回溯时间（小时），默认 7 天
            
        Returns:
            List[dict]: 发现的潜在依赖关系列表
        """
        try:
            from core.models import WebhookEvent
            from collections import Counter
            
            # 查询时间范围内的告警
            time_threshold = datetime.now() - timedelta(hours=lookback_hours)
            events = session.query(WebhookEvent).filter(
                WebhookEvent.timestamp >= time_threshold
            ).order_by(WebhookEvent.timestamp).all()
            
            if len(events) < 10:
                logger.info("告警数据不足，跳过自动发现")
                return []
            
            # 分析时间相关性
            # 统计: (service_a, service_b) -> [(time_delta, ...)]
            correlations: Dict[Tuple[str, str], List[float]] = defaultdict(list)
            
            # 用 5 分钟窗口检测连续告警
            window_seconds = 300
            
            for i, event_a in enumerate(events):
                source_a = (event_a.source or 'unknown').strip().lower()
                if not source_a or source_a == 'unknown':
                    continue
                
                # 检查后续 5 分钟内的告警
                for j in range(i + 1, len(events)):
                    event_b = events[j]
                    source_b = (event_b.source or 'unknown').strip().lower()
                    
                    if not source_b or source_b == 'unknown' or source_a == source_b:
                        continue
                    
                    time_delta = (event_b.timestamp - event_a.timestamp).total_seconds()
                    
                    if time_delta > window_seconds:
                        break
                    
                    if time_delta > 0:
                        # A 在 B 之前发生
                        correlations[(source_a, source_b)].append(time_delta)
            
            # 分析相关性并生成候选依赖
            discovered = []
            min_occurrences = 3  # 最少出现次数
            
            for (service_a, service_b), time_deltas in correlations.items():
                if len(time_deltas) < min_occurrences:
                    continue
                
                avg_delta = sum(time_deltas) / len(time_deltas)
                confidence = min(1.0, len(time_deltas) / 10)  # 出现 10 次以上置信度为 1
                
                discovered.append({
                    'service': service_b,
                    'depends_on': service_a,
                    'co_occurrence_count': len(time_deltas),
                    'avg_time_delta': round(avg_delta, 2),
                    'confidence': round(confidence, 2)
                })
            
            # 按置信度排序
            discovered.sort(key=lambda x: x['confidence'], reverse=True)
            
            logger.info(f"自动发现 {len(discovered)} 个潜在服务依赖关系")
            return discovered
            
        except Exception as e:
            logger.error(f"自动发现服务依赖失败: {e}")
            return []
    
    def ensure_loaded(self, session) -> None:
        """确保拓扑已从数据库加载"""
        if not self._loaded:
            self.load_from_db(session)
    
    def clear(self) -> None:
        """清空内存中的拓扑数据"""
        self._graph.clear()
        self._reverse_graph.clear()
        self._loaded = False
        logger.info("已清空内存中的拓扑数据")


# 全局单例
topology_manager = ServiceTopology()
