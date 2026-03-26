"""
Runbook 数据模型定义

使用 Python dataclass 定义 Runbook DSL 的数据结构，
不依赖 ORM，纯内存对象。
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class RunbookCondition:
    """
    触发条件
    
    用于定义告警匹配的指标条件，如 cpu_usage > 90
    
    Attributes:
        metric: 指标名称，如 'cpu_usage', 'disk_usage', 'restart_count'
        operator: 比较运算符，支持 '>', '<', '>=', '<=', '==', '!='
        value: 阈值，可以是数字或字符串
    """
    metric: str
    operator: str  # '>', '<', '>=', '<=', '==', '!='
    value: Any
    
    def evaluate(self, actual_value: Any) -> bool:
        """
        评估条件是否满足
        
        Args:
            actual_value: 实际的指标值
            
        Returns:
            bool: 条件是否满足
        """
        try:
            # 尝试将两边都转换为数字进行比较
            if isinstance(self.value, (int, float)) or (isinstance(self.value, str) and self.value.replace('.', '').isdigit()):
                actual = float(actual_value) if actual_value is not None else 0
                expected = float(self.value)
            else:
                actual = actual_value
                expected = self.value
            
            ops = {
                '>': lambda a, b: a > b,
                '<': lambda a, b: a < b,
                '>=': lambda a, b: a >= b,
                '<=': lambda a, b: a <= b,
                '==': lambda a, b: a == b,
                '!=': lambda a, b: a != b,
            }
            
            if self.operator not in ops:
                logger.warning(f"未知运算符: {self.operator}")
                return False
                
            return ops[self.operator](actual, expected)
        except (ValueError, TypeError) as e:
            logger.warning(f"条件评估失败: {e}, metric={self.metric}, value={actual_value}")
            return False


@dataclass
class RunbookTrigger:
    """
    触发器定义
    
    定义何时触发 Runbook 执行
    
    Attributes:
        alert_type: 告警类型，如 'cpu_high', 'disk_full', 'pod_crash'
        severity: 严重级别列表，如 ['critical', 'warning']
        conditions: 附加条件列表，所有条件需同时满足
    """
    alert_type: str
    severity: List[str] = field(default_factory=list)
    conditions: List[RunbookCondition] = field(default_factory=list)


@dataclass
class RunbookSafety:
    """
    安全控制配置
    
    定义执行 Runbook 时的安全约束
    
    Attributes:
        require_approval: 是否需要人工审批
        dry_run: 是否为试运行模式（只模拟，不实际执行）
        max_retries: 最大重试次数
        rollback_on_failure: 失败时是否回滚
        timeout: 总执行超时时间（秒）
    """
    require_approval: bool = True
    dry_run: bool = False
    max_retries: int = 2
    rollback_on_failure: bool = True
    timeout: int = 600  # 秒


@dataclass
class RunbookStep:
    """
    执行步骤定义
    
    定义 Runbook 中的单个执行步骤
    
    Attributes:
        action: 动作类型，如 'kubectl_scale', 'script', 'notify' 等
        params: 动作参数，键值对
        duration: 等待时长（仅用于 'wait' 动作）
        timeout: 步骤超时时间（仅用于 'verify' 类动作）
        on_failure: 失败时的处理策略：'abort', 'continue', 'rollback'
    """
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    duration: Optional[int] = None  # for 'wait' action
    timeout: Optional[int] = None  # for 'verify' action
    on_failure: str = 'abort'  # 'abort', 'continue', 'rollback'


@dataclass
class Runbook:
    """
    完整 Runbook 定义
    
    代表一个完整的自动修复剧本，包含触发条件、安全控制和执行步骤
    
    Attributes:
        name: Runbook 唯一名称
        description: 描述信息
        trigger: 触发器定义
        safety: 安全控制配置
        steps: 执行步骤列表
        version: 版本号
    """
    name: str
    description: str = ''
    trigger: Optional[RunbookTrigger] = None
    safety: RunbookSafety = field(default_factory=RunbookSafety)
    steps: List[RunbookStep] = field(default_factory=list)
    version: str = '1.0'
    
    def matches_alert(self, alert_data: dict) -> bool:
        """
        判断告警是否匹配此 Runbook 的触发条件
        
        匹配规则：
        1. alert_type 必须匹配
        2. 如果定义了 severity 列表，告警的 severity 必须在列表中
        3. 如果定义了 conditions，所有条件必须满足
        
        Args:
            alert_data: 告警数据字典，应包含以下字段：
                - alert_type: 告警类型
                - severity: 严重级别 (可选)
                - labels: 标签字典 (可选)
                - metrics: 指标字典 (可选，用于条件评估)
                
        Returns:
            bool: 是否匹配
        """
        if self.trigger is None:
            logger.debug(f"Runbook {self.name} 无触发器定义，不匹配任何告警")
            return False
        
        # 1. 检查 alert_type
        alert_type = alert_data.get('alert_type') or alert_data.get('alertname') or alert_data.get('type', '')
        if alert_type != self.trigger.alert_type:
            logger.debug(f"Runbook {self.name}: alert_type 不匹配 (期望={self.trigger.alert_type}, 实际={alert_type})")
            return False
        
        # 2. 检查 severity（如果定义了）
        if self.trigger.severity:
            alert_severity = alert_data.get('severity', '').lower()
            if alert_severity not in [s.lower() for s in self.trigger.severity]:
                logger.debug(f"Runbook {self.name}: severity 不匹配 (期望={self.trigger.severity}, 实际={alert_severity})")
                return False
        
        # 3. 检查 conditions（如果定义了）
        if self.trigger.conditions:
            # 从多个可能的位置获取指标值
            metrics = {}
            metrics.update(alert_data.get('metrics', {}))
            metrics.update(alert_data.get('labels', {}))
            metrics.update(alert_data.get('annotations', {}))
            # 直接从顶层获取
            for key in ['cpu_usage', 'disk_usage', 'memory_usage', 'restart_count', 'value']:
                if key in alert_data:
                    metrics[key] = alert_data[key]
            
            for condition in self.trigger.conditions:
                actual_value = metrics.get(condition.metric)
                if actual_value is None:
                    logger.debug(f"Runbook {self.name}: 条件指标 {condition.metric} 在告警中不存在")
                    return False
                if not condition.evaluate(actual_value):
                    logger.debug(f"Runbook {self.name}: 条件不满足 ({condition.metric} {condition.operator} {condition.value}, 实际={actual_value})")
                    return False
        
        logger.info(f"Runbook {self.name} 匹配告警: alert_type={alert_type}")
        return True
    
    def __repr__(self) -> str:
        return f"Runbook(name={self.name!r}, version={self.version!r}, steps={len(self.steps)})"
