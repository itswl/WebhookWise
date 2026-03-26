"""
YAML Runbook 解析器

负责加载、解析和管理 Runbook YAML 文件。
支持热重载、验证和匹配查询。
"""

import yaml
import os
import logging
from typing import Dict, List, Optional

from .models import (
    Runbook,
    RunbookTrigger,
    RunbookCondition,
    RunbookSafety,
    RunbookStep
)

logger = logging.getLogger(__name__)

# 已知的 action 类型
KNOWN_ACTIONS = {
    'kubectl_scale',
    'kubectl_restart',
    'kubectl_logs',
    'script',
    'http_request',
    'wait',
    'verify_alert_resolved',
    'notify',
}


class RunbookParseError(Exception):
    """Runbook 解析错误"""
    pass


class RunbookParser:
    """
    YAML Runbook 解析和管理
    
    负责从文件系统加载 Runbook YAML 文件，解析为 Runbook 对象，
    并提供查询和匹配功能。
    
    Attributes:
        runbooks_dir: Runbook YAML 文件所在目录
    """
    
    def __init__(self, runbooks_dir: str = None):
        """
        初始化解析器
        
        Args:
            runbooks_dir: Runbook 目录路径，默认为项目根目录下的 runbooks/
        """
        if runbooks_dir is None:
            # 默认路径：项目根目录/runbooks
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            runbooks_dir = os.path.join(base_dir, 'runbooks')
        
        self.runbooks_dir = runbooks_dir
        self._runbooks: Dict[str, Runbook] = {}
        self._load_all()
    
    def _load_all(self):
        """
        加载 runbooks 目录下所有 YAML 文件
        
        遍历目录加载所有 .yaml 和 .yml 文件，
        解析失败的文件会记录错误日志但不影响其他文件加载。
        """
        self._runbooks.clear()
        
        if not os.path.exists(self.runbooks_dir):
            logger.warning(f"Runbooks 目录不存在: {self.runbooks_dir}")
            return
        
        if not os.path.isdir(self.runbooks_dir):
            logger.error(f"Runbooks 路径不是目录: {self.runbooks_dir}")
            return
        
        loaded_count = 0
        error_count = 0
        
        for filename in os.listdir(self.runbooks_dir):
            if not filename.endswith(('.yaml', '.yml')):
                continue
            
            filepath = os.path.join(self.runbooks_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                runbook = self.parse_yaml(content)
                
                # 验证 runbook
                errors = self.validate_runbook(runbook)
                if errors:
                    logger.warning(f"Runbook {filename} 验证警告: {errors}")
                
                self._runbooks[runbook.name] = runbook
                loaded_count += 1
                logger.info(f"已加载 Runbook: {runbook.name} (from {filename})")
                
            except yaml.YAMLError as e:
                logger.error(f"YAML 解析错误 {filename}: {e}")
                error_count += 1
            except RunbookParseError as e:
                logger.error(f"Runbook 解析错误 {filename}: {e}")
                error_count += 1
            except IOError as e:
                logger.error(f"文件读取错误 {filename}: {e}")
                error_count += 1
            except Exception as e:
                logger.error(f"加载 Runbook 失败 {filename}: {e}")
                error_count += 1
        
        logger.info(f"Runbook 加载完成: 成功 {loaded_count}, 失败 {error_count}")
    
    def parse_yaml(self, yaml_content: str) -> Runbook:
        """
        解析单个 YAML 内容为 Runbook 对象
        
        Args:
            yaml_content: YAML 格式的字符串
            
        Returns:
            Runbook: 解析后的 Runbook 对象
            
        Raises:
            RunbookParseError: 解析失败时抛出
            yaml.YAMLError: YAML 格式错误时抛出
        """
        data = yaml.safe_load(yaml_content)
        
        if not isinstance(data, dict):
            raise RunbookParseError("Runbook 必须是 YAML 字典格式")
        
        # 必需字段：name
        name = data.get('name')
        if not name:
            raise RunbookParseError("Runbook 必须包含 'name' 字段")
        
        # 解析 trigger
        trigger = None
        if 'trigger' in data:
            trigger = self._parse_trigger(data['trigger'])
        
        # 解析 safety
        safety = RunbookSafety()
        if 'safety' in data:
            safety = self._parse_safety(data['safety'])
        
        # 解析 steps
        steps = []
        if 'steps' in data:
            steps = self._parse_steps(data['steps'])
        
        return Runbook(
            name=name,
            description=data.get('description', ''),
            trigger=trigger,
            safety=safety,
            steps=steps,
            version=str(data.get('version', '1.0'))
        )
    
    def _parse_trigger(self, trigger_data: dict) -> RunbookTrigger:
        """解析触发器定义"""
        if not isinstance(trigger_data, dict):
            raise RunbookParseError("trigger 必须是字典格式")
        
        alert_type = trigger_data.get('alert_type', '')
        severity = trigger_data.get('severity', [])
        
        # 确保 severity 是列表
        if isinstance(severity, str):
            severity = [severity]
        
        # 解析 conditions
        conditions = []
        for cond_data in trigger_data.get('conditions', []):
            if not isinstance(cond_data, dict):
                continue
            conditions.append(RunbookCondition(
                metric=cond_data.get('metric', ''),
                operator=cond_data.get('operator', '=='),
                value=cond_data.get('value')
            ))
        
        return RunbookTrigger(
            alert_type=alert_type,
            severity=severity,
            conditions=conditions
        )
    
    def _parse_safety(self, safety_data: dict) -> RunbookSafety:
        """解析安全控制配置"""
        if not isinstance(safety_data, dict):
            raise RunbookParseError("safety 必须是字典格式")
        
        return RunbookSafety(
            require_approval=safety_data.get('require_approval', True),
            dry_run=safety_data.get('dry_run', False),
            max_retries=safety_data.get('max_retries', 2),
            rollback_on_failure=safety_data.get('rollback_on_failure', True),
            timeout=safety_data.get('timeout', 600)
        )
    
    def _parse_steps(self, steps_data: list) -> List[RunbookStep]:
        """解析执行步骤列表"""
        if not isinstance(steps_data, list):
            raise RunbookParseError("steps 必须是列表格式")
        
        steps = []
        for step_data in steps_data:
            if not isinstance(step_data, dict):
                continue
            
            action = step_data.get('action', '')
            if not action:
                logger.warning("步骤缺少 action 字段，跳过")
                continue
            
            steps.append(RunbookStep(
                action=action,
                params=step_data.get('params', {}),
                duration=step_data.get('duration'),
                timeout=step_data.get('timeout'),
                on_failure=step_data.get('on_failure', 'abort')
            ))
        
        return steps
    
    def get_runbook(self, name: str) -> Optional[Runbook]:
        """
        按名称获取 Runbook
        
        Args:
            name: Runbook 名称
            
        Returns:
            Runbook 对象，如果不存在返回 None
        """
        return self._runbooks.get(name)
    
    def list_runbooks(self) -> List[Runbook]:
        """
        列出所有已加载的 Runbook
        
        Returns:
            Runbook 对象列表
        """
        return list(self._runbooks.values())
    
    def find_matching_runbooks(self, alert_data: dict) -> List[Runbook]:
        """
        查找与告警匹配的所有 Runbook
        
        按匹配规则遍历所有 Runbook，返回匹配的列表。
        
        Args:
            alert_data: 告警数据字典
            
        Returns:
            匹配的 Runbook 列表
        """
        matching = []
        for runbook in self._runbooks.values():
            try:
                if runbook.matches_alert(alert_data):
                    matching.append(runbook)
            except Exception as e:
                logger.error(f"Runbook {runbook.name} 匹配检查失败: {e}")
        
        logger.debug(f"告警匹配到 {len(matching)} 个 Runbook")
        return matching
    
    def reload(self):
        """
        热重载所有 Runbook
        
        重新从文件系统加载所有 Runbook，
        适用于配置变更后的动态更新。
        """
        logger.info("开始热重载 Runbooks...")
        self._load_all()
        logger.info(f"热重载完成，当前加载 {len(self._runbooks)} 个 Runbook")
    
    def validate_runbook(self, runbook: Runbook) -> List[str]:
        """
        验证 Runbook 配置的完整性
        
        检查项：
        1. name 非空
        2. steps 非空
        3. action 是已知类型
        4. on_failure 是有效值
        5. wait 动作有 duration
        6. verify 动作有 timeout
        
        Args:
            runbook: 要验证的 Runbook 对象
            
        Returns:
            错误信息列表，空列表表示验证通过
        """
        errors = []
        
        # 1. 检查 name
        if not runbook.name or not runbook.name.strip():
            errors.append("name 不能为空")
        
        # 2. 检查 steps
        if not runbook.steps:
            errors.append("steps 不能为空")
        
        # 3. 检查每个 step
        valid_on_failure = {'abort', 'continue', 'rollback'}
        
        for i, step in enumerate(runbook.steps):
            step_prefix = f"步骤 {i + 1}"
            
            # 检查 action 是否为已知类型
            if step.action not in KNOWN_ACTIONS:
                errors.append(f"{step_prefix}: 未知的 action 类型 '{step.action}' (已知类型: {', '.join(sorted(KNOWN_ACTIONS))})")
            
            # 检查 on_failure 是否有效
            if step.on_failure not in valid_on_failure:
                errors.append(f"{step_prefix}: 无效的 on_failure 值 '{step.on_failure}' (有效值: {', '.join(valid_on_failure)})")
            
            # 检查 wait 动作是否有 duration
            if step.action == 'wait' and step.duration is None:
                errors.append(f"{step_prefix}: wait 动作必须指定 duration")
            
            # 检查 verify 动作是否有 timeout
            if step.action == 'verify_alert_resolved' and step.timeout is None:
                errors.append(f"{step_prefix}: verify_alert_resolved 动作建议指定 timeout")
        
        # 4. 检查 trigger（如果有）
        if runbook.trigger:
            if not runbook.trigger.alert_type:
                errors.append("trigger.alert_type 不能为空")
        
        return errors
    
    def __len__(self) -> int:
        """返回已加载的 Runbook 数量"""
        return len(self._runbooks)
    
    def __contains__(self, name: str) -> bool:
        """检查是否包含指定名称的 Runbook"""
        return name in self._runbooks
