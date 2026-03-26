"""
修复执行引擎核心

负责安全地执行 Runbook，包括：
- 模板变量渲染
- 执行状态管理
- 审批流程
- 执行历史记录
"""

import logging
import json
import time
import uuid
import re
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any

from .models import Runbook, RunbookStep
from .runbook_parser import RunbookParser
from .actions import ActionRegistry, ActionResult

logger = logging.getLogger(__name__)


class ExecutionStatus:
    """执行状态常量"""
    PENDING = 'pending'
    AWAITING_APPROVAL = 'awaiting_approval'
    RUNNING = 'running'
    SUCCESS = 'success'
    FAILED = 'failed'
    ROLLED_BACK = 'rolled_back'
    DRY_RUN = 'dry_run_complete'


class RemediationEngine:
    """
    自愈执行引擎 - 安全地执行 Runbook
    
    功能：
    - 查找匹配告警的 Runbook
    - 渲染模板变量
    - 逐步执行 Runbook 步骤
    - 支持审批流程
    - 支持 dry-run 模式
    - 记录执行历史到数据库
    """
    
    def __init__(self, runbook_parser: RunbookParser = None):
        """
        初始化执行引擎
        
        Args:
            runbook_parser: Runbook 解析器实例，默认自动创建
        """
        self.parser = runbook_parser or RunbookParser()
        self.action_registry = ActionRegistry()
        self._executions: Dict[str, dict] = {}  # 内存中的执行状态缓存
        self._lock = threading.Lock()
    
    def execute_runbook(
        self, 
        runbook_name: str, 
        alert_data: dict = None, 
        dry_run: bool = False, 
        force: bool = False,
        alert_id: int = None
    ) -> dict:
        """
        执行指定 Runbook
        
        Args:
            runbook_name: Runbook 名称
            alert_data: 触发告警数据（优先使用）
            dry_run: 是否干运行模式（只模拟不实际执行）
            force: 是否强制执行（跳过审批）
            alert_id: 告警 ID，如果未提供 alert_data，则从数据库查询对应告警数据
        
        Returns:
            dict: 执行记录，包含 execution_id、status、steps_log 等
        """
        execution_id = str(uuid.uuid4())
        
        # 修复：处理 alert_data 和 alert_id 参数
        # 注意：alert_data 可能是空字典 {}（手动执行时），需要用 is not None 判断
        if alert_data is not None:
            logger.info(f"开始执行 Runbook: {runbook_name}, execution_id={execution_id}, dry_run={dry_run}, force={force}, 使用请求提供的 alert_data")
        elif alert_id:
            # 从数据库查询告警数据
            alert_data = self._get_alert_data_by_id(alert_id)
            if alert_data is None:
                logger.error(f"Runbook 执行失败: 找不到 alert_id={alert_id} 的告警数据")
                return self._create_execution_record(
                    execution_id=execution_id,
                    runbook_name=runbook_name,
                    alert_data={},
                    status=ExecutionStatus.FAILED,
                    error_message=f"找不到 alert_id={alert_id} 的告警数据",
                    dry_run=dry_run
                )
            logger.info(f"开始执行 Runbook: {runbook_name}, execution_id={execution_id}, dry_run={dry_run}, force={force}, 使用 alert_id={alert_id} 的告警数据")
        else:
            # 既没有 alert_data 也没有 alert_id，允许手动执行（使用空字典）
            alert_data = {}
            logger.info(f"开始执行 Runbook: {runbook_name}, execution_id={execution_id}, dry_run={dry_run}, force={force}, 手动触发（无关联告警）")
        
        # 1. 获取 Runbook
        runbook = self.parser.get_runbook(runbook_name)
        if not runbook:
            logger.error(f"Runbook 不存在: {runbook_name}")
            return self._create_execution_record(
                execution_id=execution_id,
                runbook_name=runbook_name,
                alert_data=alert_data,
                status=ExecutionStatus.FAILED,
                error_message=f"Runbook 不存在: {runbook_name}",
                dry_run=dry_run
            )
        
        # 2. 检查安全控制
        if runbook.safety.require_approval and not force and not dry_run:
            logger.info(f"Runbook {runbook_name} 需要审批，等待人工确认")
            execution = self._create_execution_record(
                execution_id=execution_id,
                runbook_name=runbook_name,
                alert_data=alert_data,
                status=ExecutionStatus.AWAITING_APPROVAL,
                dry_run=dry_run
            )
            # 保存到内存缓存，等待审批
            with self._lock:
                self._executions[execution_id] = {
                    'record': execution,
                    'runbook': runbook,
                    'alert_data': alert_data,
                    'dry_run': dry_run
                }
            return execution
        
        # 3. 执行 Runbook
        return self._do_execute(execution_id, runbook, alert_data, dry_run)
    
    def _do_execute(
        self, 
        execution_id: str, 
        runbook: Runbook, 
        alert_data: dict, 
        dry_run: bool
    ) -> dict:
        """
        实际执行 Runbook
        
        Args:
            execution_id: 执行 ID
            runbook: Runbook 对象
            alert_data: 告警数据
            dry_run: 是否干运行
            
        Returns:
            dict: 执行记录
        """
        # 构建模板渲染上下文
        context = self._build_context(alert_data)
        
        steps_log = []
        overall_status = ExecutionStatus.SUCCESS if not dry_run else ExecutionStatus.DRY_RUN
        error_message = None
        
        start_time = time.time()
        
        for i, step in enumerate(runbook.steps):
            step_num = i + 1
            logger.info(f"执行步骤 {step_num}/{len(runbook.steps)}: {step.action}")
            
            # 渲染模板参数（传入 dry_run 参数）
            rendered_params = self._render_params(step.params, context, dry_run)
            
            # 修复：检查是否有未解析的模板变量（非 dry_run 模式下）
            if not dry_run:
                unresolved = self._check_unresolved_templates(rendered_params)
                if unresolved:
                    error_msg = f"步骤 {step_num} 参数缺失: 无法解析模板变量 {', '.join(unresolved)}"
                    logger.error(error_msg)
                    step_log = {
                        'step': step_num,
                        'action': step.action,
                        'params': rendered_params,
                        'status': 'failed',
                        'output': '',
                        'error': error_msg,
                        'duration': 0
                    }
                    steps_log.append(step_log)
                    overall_status = ExecutionStatus.FAILED
                    error_message = error_msg
                    break
            
            # 处理特殊参数
            if step.action == 'wait' and step.duration:
                rendered_params['duration'] = step.duration
            if step.action == 'verify_alert_resolved':
                rendered_params['alert_hash'] = alert_data.get('alert_hash')
                if step.timeout:
                    rendered_params['timeout'] = step.timeout
            
            # 执行步骤
            step_start = time.time()
            result = self._execute_step(step, rendered_params, context, dry_run)
            step_duration = time.time() - step_start
            
            step_log = {
                'step': step_num,
                'action': step.action,
                'params': rendered_params,
                'status': 'success' if result.success else 'failed',
                'output': result.output,
                'error': result.error,
                'duration': round(step_duration, 2)
            }
            steps_log.append(step_log)
            
            # 处理失败
            if not result.success:
                logger.warning(f"步骤 {step_num} 执行失败: {result.error}")
                
                if step.on_failure == 'abort':
                    overall_status = ExecutionStatus.FAILED
                    error_message = f"步骤 {step_num} ({step.action}) 执行失败: {result.error}"
                    logger.error(f"Runbook 执行中止: {error_message}")
                    break
                elif step.on_failure == 'rollback':
                    overall_status = ExecutionStatus.ROLLED_BACK
                    error_message = f"步骤 {step_num} ({step.action}) 触发回滚: {result.error}"
                    logger.warning(f"触发回滚: {error_message}")
                    # TODO: 实际执行回滚逻辑
                    break
                elif step.on_failure == 'continue':
                    logger.info(f"步骤 {step_num} 失败但继续执行 (on_failure=continue)")
                    continue
        
        total_duration = time.time() - start_time
        
        # 创建执行记录
        execution = self._create_execution_record(
            execution_id=execution_id,
            runbook_name=runbook.name,
            alert_data=alert_data,
            status=overall_status,
            steps_log=steps_log,
            error_message=error_message,
            dry_run=dry_run
        )
        execution['duration'] = round(total_duration, 2)
        
        # 保存到数据库
        self._save_execution_to_db(execution, alert_data)
        
        logger.info(f"Runbook {runbook.name} 执行完成: status={overall_status}, duration={total_duration:.2f}s")
        return execution
    
    def _get_alert_data_by_id(self, alert_id: int) -> Optional[dict]:
        """
        根据告警 ID 从数据库查询告警数据
        
        Args:
            alert_id: 告警 ID (webhook event id)
            
        Returns:
            dict: 告警数据，如果找不到返回 None
        """
        try:
            from core.models import get_session, WebhookEvent
            
            session = get_session()
            try:
                event = session.query(WebhookEvent).filter_by(id=alert_id).first()
                if not event:
                    return None
                
                # 构建告警数据结构
                alert_data = {
                    'alert_id': event.id,
                    'source': event.source,
                    'parsed_data': event.parsed_data or {},
                    'raw_payload': event.raw_payload,
                    'alert_hash': event.alert_hash,
                    'timestamp': event.timestamp.isoformat() if event.timestamp else None,
                    'client_ip': event.client_ip
                }
                
                # 提取 labels 到顶层方便模板使用
                if event.parsed_data:
                    parsed = event.parsed_data
                    if 'labels' in parsed:
                        alert_data['labels'] = parsed['labels']
                    # Prometheus Alertmanager 格式
                    if 'alerts' in parsed and len(parsed.get('alerts', [])) > 0:
                        first_alert = parsed['alerts'][0]
                        alert_data['labels'] = first_alert.get('labels', {})
                        alert_data['annotations'] = first_alert.get('annotations', {})
                
                return alert_data
            finally:
                session.close()
        except Exception as e:
            logger.error(f"查询告警数据失败: {e}", exc_info=True)
            return None
    
    def _build_context(self, alert_data: dict) -> dict:
        """
        构建模板渲染上下文
        
        将告警数据转换为模板可访问的上下文结构。
        
        Args:
            alert_data: 原始告警数据
            
        Returns:
            dict: 模板上下文
        """
        # 基础上下文
        context = {
            'alert': {
                'labels': {},
                'annotations': {},
                'metrics': {}
            }
        }
        
        # 提取 labels
        if 'labels' in alert_data:
            context['alert']['labels'] = alert_data['labels']
        elif 'parsed_data' in alert_data:
            parsed = alert_data['parsed_data']
            # Prometheus Alertmanager 格式
            if 'alerts' in parsed and len(parsed.get('alerts', [])) > 0:
                first_alert = parsed['alerts'][0]
                context['alert']['labels'] = first_alert.get('labels', {})
                context['alert']['annotations'] = first_alert.get('annotations', {})
            # 通用格式
            elif 'labels' in parsed:
                context['alert']['labels'] = parsed['labels']
        
        # 直接暴露的字段
        for key in ['deployment', 'namespace', 'pod', 'service', 'alertname', 'severity']:
            if key in alert_data:
                context['alert']['labels'][key] = alert_data[key]
            elif 'parsed_data' in alert_data and key in alert_data['parsed_data']:
                context['alert']['labels'][key] = alert_data['parsed_data'][key]
        
        # 指标数据
        if 'metrics' in alert_data:
            context['alert']['metrics'] = alert_data['metrics']
        
        return context
    
    def _render_template(self, value: str, context: dict, dry_run: bool = False) -> str:
        """
        渲染模板变量
        
        支持的语法：
        - {{ var }} - 简单变量替换
        - {{ var | default('xxx') }} - 带默认值的变量
        - 支持嵌套访问如 {{ alert.labels.deployment }}
        
        修复：如果模板变量无法解析且没有默认值，保留原始模板文本作为占位符
        （仅在 dry_run 模式下，非 dry_run 模式下返回空字符串保持向后兼容）
        
        Args:
            value: 模板字符串
            context: 上下文字典
            dry_run: 是否为干运行模式
            
        Returns:
            str: 渲染后的字符串
        """
        if not isinstance(value, str):
            return value
        
        # 匹配 {{ ... }} 模式
        pattern = r'\{\{\s*(.+?)\s*\}\}'
        
        def replace_var(match):
            expr = match.group(1).strip()
            original_expr = match.group(0)  # 保留原始模板文本 {{ ... }}
            
            # 检查是否有 default filter
            default_value = None
            if '|' in expr:
                parts = expr.split('|', 1)
                expr = parts[0].strip()
                filter_part = parts[1].strip()
                
                # 解析 default('xxx') 或 default("xxx")
                default_match = re.match(r"default\s*\(\s*['\"](.+?)['\"]\s*\)", filter_part)
                if default_match:
                    default_value = default_match.group(1)
            
            # 解析嵌套变量路径
            result = self._get_nested_value(context, expr)
            if result is None:
                # 修复：如果有默认值，使用默认值；否则保留原始模板文本
                if default_value is not None:
                    return default_value
                # 保留原始模板文本作为占位符，以便后续检查
                return original_expr
            return str(result)
        
        return re.sub(pattern, replace_var, value)
    
    def _get_nested_value(self, data: dict, path: str) -> Optional[Any]:
        """
        获取嵌套字典中的值
        
        Args:
            data: 数据字典
            path: 点分隔的路径，如 'alert.labels.deployment'
            
        Returns:
            值，如果不存在返回 None
        """
        keys = path.split('.')
        current = data
        
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        
        return current
    
    def _render_params(self, params: dict, context: dict, dry_run: bool = False) -> dict:
        """
        渲染参数字典中的所有模板变量
        
        Args:
            params: 参数字典
            context: 上下文
            dry_run: 是否为干运行模式
            
        Returns:
            dict: 渲染后的参数字典
        """
        rendered = {}
        for key, value in params.items():
            if isinstance(value, str):
                rendered[key] = self._render_template(value, context, dry_run)
            elif isinstance(value, dict):
                rendered[key] = self._render_params(value, context, dry_run)
            elif isinstance(value, list):
                rendered[key] = [
                    self._render_template(v, context, dry_run) if isinstance(v, str) else v 
                    for v in value
                ]
            else:
                rendered[key] = value
        return rendered
    
    def _check_unresolved_templates(self, params: dict) -> List[str]:
        """
        检查渲染后的参数中是否还有未解析的模板变量
        
        Args:
            params: 渲染后的参数字典
            
        Returns:
            List[str]: 未解析的模板变量列表
        """
        unresolved = []
        pattern = r'\{\{\s*(.+?)\s*\}\}'
        
        def check_value(value):
            if isinstance(value, str):
                matches = re.findall(pattern, value)
                if matches:
                    unresolved.extend(matches)
            elif isinstance(value, dict):
                for v in value.values():
                    check_value(v)
            elif isinstance(value, list):
                for v in value:
                    check_value(v)
        
        check_value(params)
        return unresolved
    
    def _execute_step(
        self, 
        step: RunbookStep, 
        params: dict, 
        context: dict, 
        dry_run: bool
    ) -> ActionResult:
        """
        执行单个步骤
        
        Args:
            step: 步骤定义
            params: 渲染后的参数
            context: 上下文
            dry_run: 是否干运行
            
        Returns:
            ActionResult: 执行结果
        """
        return self.action_registry.execute(step.action, params, dry_run=dry_run)
    
    def _create_execution_record(
        self, 
        execution_id: str,
        runbook_name: str,
        alert_data: dict,
        status: str,
        steps_log: List[dict] = None,
        error_message: str = None,
        dry_run: bool = False
    ) -> dict:
        """
        创建执行记录
        
        Args:
            execution_id: 执行 ID
            runbook_name: Runbook 名称
            alert_data: 告警数据
            status: 执行状态
            steps_log: 步骤执行日志
            error_message: 错误信息
            dry_run: 是否为干运行
            
        Returns:
            dict: 执行记录
        """
        now = datetime.now()
        record = {
            'execution_id': execution_id,
            'runbook_name': runbook_name,
            'status': status,
            'steps_log': steps_log or [],
            'dry_run': dry_run,
            'started_at': now.isoformat(),
            'error_message': error_message,
            'trigger_alert_hash': alert_data.get('alert_hash')
        }
        
        if status in [ExecutionStatus.SUCCESS, ExecutionStatus.FAILED, 
                      ExecutionStatus.ROLLED_BACK, ExecutionStatus.DRY_RUN]:
            record['completed_at'] = now.isoformat()
        
        return record
    
    def _save_execution_to_db(self, execution: dict, alert_data: dict) -> bool:
        """
        保存执行记录到数据库
        
        Args:
            execution: 执行记录
            alert_data: 告警数据
            
        Returns:
            bool: 是否保存成功
        """
        try:
            from core.models import get_session
            
            # 动态导入以避免循环依赖
            from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
            from sqlalchemy import func
            
            session = get_session()
            try:
                # 检查表是否存在（通过尝试查询）
                from core.models import Base
                from sqlalchemy import inspect
                
                inspector = inspect(session.bind)
                if 'remediation_execution' not in inspector.get_table_names():
                    logger.warning("remediation_execution 表不存在，跳过数据库保存")
                    return False
                
                # 使用 ORM 模型
                from core.models import RemediationExecution
                
                db_record = RemediationExecution(
                    execution_id=execution['execution_id'],
                    runbook_name=execution['runbook_name'],
                    trigger_alert_hash=execution.get('trigger_alert_hash'),
                    status=execution['status'],
                    steps_log=json.dumps(execution.get('steps_log', []), ensure_ascii=False),
                    dry_run=execution.get('dry_run', False),
                    error_message=execution.get('error_message')
                )
                
                if execution.get('completed_at'):
                    db_record.completed_at = datetime.fromisoformat(execution['completed_at'])
                
                session.add(db_record)
                session.commit()
                logger.info(f"执行记录已保存到数据库: {execution['execution_id']}")
                return True
                
            finally:
                session.close()
                
        except ImportError as e:
            logger.warning(f"无法导入数据库模型，跳过保存: {e}")
            return False
        except Exception as e:
            logger.error(f"保存执行记录到数据库失败: {e}", exc_info=True)
            return False
    
    def approve_execution(self, execution_id: str) -> dict:
        """
        审批并继续执行
        
        Args:
            execution_id: 执行 ID
            
        Returns:
            dict: 执行结果
        """
        with self._lock:
            if execution_id not in self._executions:
                logger.error(f"执行记录不存在或已过期: {execution_id}")
                return {
                    'success': False,
                    'error': f'执行记录不存在或已过期: {execution_id}'
                }
            
            cached = self._executions.pop(execution_id)
        
        record = cached['record']
        if record['status'] != ExecutionStatus.AWAITING_APPROVAL:
            return {
                'success': False,
                'error': f'执行状态不是等待审批: {record["status"]}'
            }
        
        logger.info(f"审批通过，继续执行: {execution_id}")
        
        # 继续执行
        result = self._do_execute(
            execution_id,
            cached['runbook'],
            cached['alert_data'],
            cached['dry_run']
        )
        
        return {
            'success': True,
            'execution': result
        }
    
    def get_execution(self, execution_id: str) -> Optional[dict]:
        """
        获取执行记录
        
        先从内存缓存查找，再从数据库查找。
        
        Args:
            execution_id: 执行 ID
            
        Returns:
            dict: 执行记录，不存在返回 None
        """
        # 先检查内存缓存
        with self._lock:
            if execution_id in self._executions:
                return self._executions[execution_id]['record']
        
        # 从数据库查询
        try:
            from core.models import get_session
            
            session = get_session()
            try:
                from sqlalchemy import inspect
                inspector = inspect(session.bind)
                if 'remediation_execution' not in inspector.get_table_names():
                    return None
                
                from core.models import RemediationExecution
                
                record = session.query(RemediationExecution).filter(
                    RemediationExecution.execution_id == execution_id
                ).first()
                
                if not record:
                    return None
                
                return {
                    'execution_id': record.execution_id,
                    'runbook_name': record.runbook_name,
                    'status': record.status,
                    'steps_log': json.loads(record.steps_log) if record.steps_log else [],
                    'dry_run': record.dry_run,
                    'started_at': record.started_at.isoformat() if record.started_at else None,
                    'completed_at': record.completed_at.isoformat() if record.completed_at else None,
                    'error_message': record.error_message,
                    'trigger_alert_hash': record.trigger_alert_hash
                }
            finally:
                session.close()
                
        except Exception as e:
            logger.error(f"查询执行记录失败: {e}")
            return None
    
    def list_executions(self, limit: int = 50) -> List[dict]:
        """
        列出执行历史
        
        Args:
            limit: 返回数量限制
            
        Returns:
            list: 执行记录列表
        """
        executions = []
        
        # 先添加内存中等待审批的记录
        with self._lock:
            for cached in self._executions.values():
                executions.append(cached['record'])
        
        # 从数据库查询历史记录
        try:
            from core.models import get_session
            
            session = get_session()
            try:
                from sqlalchemy import inspect
                inspector = inspect(session.bind)
                if 'remediation_execution' not in inspector.get_table_names():
                    return executions
                
                from core.models import RemediationExecution
                
                db_records = session.query(RemediationExecution)\
                    .order_by(RemediationExecution.started_at.desc())\
                    .limit(limit)\
                    .all()
                
                for record in db_records:
                    executions.append({
                        'execution_id': record.execution_id,
                        'runbook_name': record.runbook_name,
                        'status': record.status,
                        'steps_log': json.loads(record.steps_log) if record.steps_log else [],
                        'dry_run': record.dry_run,
                        'started_at': record.started_at.isoformat() if record.started_at else None,
                        'completed_at': record.completed_at.isoformat() if record.completed_at else None,
                        'error_message': record.error_message,
                        'trigger_alert_hash': record.trigger_alert_hash
                    })
            finally:
                session.close()
                
        except Exception as e:
            logger.error(f"查询执行历史失败: {e}")
        
        return executions[:limit]
    
    def find_matching_runbook(self, alert_data: dict) -> Optional[str]:
        """
        根据告警数据找到匹配的 Runbook 名称
        
        遍历所有 Runbook，返回第一个匹配的名称。
        
        Args:
            alert_data: 告警数据，可能是 parsed_data 或完整告警结构
            
        Returns:
            str: 匹配的 Runbook 名称，无匹配返回 None
        """
        # 标准化告警数据结构
        normalized = self._normalize_alert_for_matching(alert_data)
        
        matching = self.parser.find_matching_runbooks(normalized)
        if matching:
            logger.info(f"找到匹配的 Runbook: {matching[0].name}")
            return matching[0].name
        
        logger.debug("未找到匹配的 Runbook")
        return None
    
    def _normalize_alert_for_matching(self, alert_data: dict) -> dict:
        """
        标准化告警数据用于 Runbook 匹配
        
        Args:
            alert_data: 原始告警数据
            
        Returns:
            dict: 标准化后的告警数据
        """
        normalized = {}
        
        # 如果是完整 webhook 结构，提取 parsed_data
        if 'parsed_data' in alert_data:
            parsed = alert_data['parsed_data']
        else:
            parsed = alert_data
        
        # 提取 alert_type
        # 可能的字段：alertname、alert_type、type、RuleName
        normalized['alert_type'] = (
            parsed.get('alertname') or
            parsed.get('alert_type') or
            parsed.get('type') or
            parsed.get('RuleName') or
            ''
        )
        
        # 提取 severity
        normalized['severity'] = (
            parsed.get('severity') or
            parsed.get('Level', '').lower() or
            ''
        )
        
        # 提取 labels
        if 'labels' in parsed:
            normalized['labels'] = parsed['labels']
            # 也从 labels 中查找 alertname
            if not normalized['alert_type'] and 'alertname' in parsed['labels']:
                normalized['alert_type'] = parsed['labels']['alertname']
        
        # Prometheus Alertmanager 格式处理
        if 'alerts' in parsed and len(parsed.get('alerts', [])) > 0:
            first_alert = parsed['alerts'][0]
            labels = first_alert.get('labels', {})
            normalized['labels'] = labels
            normalized['alert_type'] = labels.get('alertname', normalized['alert_type'])
            normalized['severity'] = labels.get('severity', normalized['severity'])
        
        # 提取指标值（用于条件判断）
        normalized['metrics'] = {}
        for key in ['cpu_usage', 'disk_usage', 'memory_usage', 'restart_count', 'value', 'CurrentValue']:
            if key in parsed:
                normalized['metrics'][key.lower()] = parsed[key]
        
        return normalized
    
    def extract_parameters_from_runbook(self, runbook) -> list:
        """
        从 Runbook 中提取所需的模板参数
        
        扫描所有 steps 中的 params，提取 {{ alert.labels.xxx }} 格式的模板变量。
        
        Args:
            runbook: Runbook 对象
            
        Returns:
            list: 参数列表，每项包含 name, required, default, steps, description
        """
        parameters = {}
        # 匹配 {{ alert.labels.param_name }} 或 {{ alert.labels.param_name | default('value') }}
        pattern = r'\{\{\s*alert\.labels\.(\w+)(?:\s*\|\s*default\s*\(\s*[\'"](.+?)[\'"]\s*\))?\s*\}\}'
        
        for step_idx, step in enumerate(runbook.steps, 1):
            params = step.params if step.params else {}
            self._scan_params_for_templates(params, pattern, parameters, step_idx)
        
        return list(parameters.values())
    
    def _scan_params_for_templates(self, params: dict, pattern: str, parameters: dict, step_idx: int):
        """
        递归扫描参数字典中的模板变量
        
        Args:
            params: 参数字典或值
            pattern: 模板变量正则表达式
            parameters: 收集到的参数字典（会被修改）
            step_idx: 当前步骤索引
        """
        if isinstance(params, str):
            matches = re.findall(pattern, params)
            for param_name, default_val in matches:
                if param_name not in parameters:
                    parameters[param_name] = {
                        'name': param_name,
                        'required': not bool(default_val),
                        'default': default_val if default_val else None,
                        'steps': [step_idx],
                        'description': self._get_param_description(param_name)
                    }
                else:
                    if step_idx not in parameters[param_name]['steps']:
                        parameters[param_name]['steps'].append(step_idx)
        elif isinstance(params, dict):
            for value in params.values():
                self._scan_params_for_templates(value, pattern, parameters, step_idx)
        elif isinstance(params, list):
            for item in params:
                self._scan_params_for_templates(item, pattern, parameters, step_idx)
    
    def _get_param_description(self, param_name: str) -> str:
        """
        返回参数的友好描述
        
        Args:
            param_name: 参数名称
            
        Returns:
            str: 参数描述
        """
        descriptions = {
            'deployment': 'Kubernetes Deployment 名称',
            'namespace': 'Kubernetes 命名空间',
            'pod': 'Pod 名称',
            'instance': '主机/实例标识符',
            'service': 'Service 名称',
            'container': '容器名称',
            'node': '节点名称',
            'alertname': '告警名称',
            'severity': '告警级别',
            'job': 'Prometheus Job 名称',
            'cluster': '集群名称',
            'app': '应用名称',
        }
        return descriptions.get(param_name, param_name)
    
    def reload_runbooks(self):
        """热重载所有 Runbook"""
        self.parser.reload()
        logger.info(f"Runbook 已重新加载，当前共 {len(self.parser)} 个")


# 全局单例
remediation_engine = RemediationEngine()
