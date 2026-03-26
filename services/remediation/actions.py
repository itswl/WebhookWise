"""
内置 Action 库

提供 Runbook 执行引擎使用的各种预定义动作，
包括 Kubernetes 操作、HTTP 请求、脚本执行等。
"""

import logging
import subprocess
import requests
import time
import re
from typing import Dict, Any, Callable, Optional

logger = logging.getLogger(__name__)

# 危险命令黑名单（用于脚本安全检查）
DANGEROUS_COMMANDS = [
    'rm -rf',
    'rm -r /',
    'rm -fr',
    'dd if=',
    'mkfs',
    ':(){:|:&};:',  # fork bomb
    '> /dev/sda',
    'chmod -R 777 /',
    'chown -R',
    'wget.*|.*sh',
    'curl.*|.*sh',
    'shutdown',
    'reboot',
    'halt',
    'poweroff',
    'init 0',
    'init 6',
]


class ActionResult:
    """Action 执行结果"""
    
    def __init__(self, success: bool, output: str = '', error: str = ''):
        self.success = success
        self.output = output
        self.error = error
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': self.success,
            'output': self.output,
            'error': self.error
        }
    
    def __repr__(self) -> str:
        status = 'SUCCESS' if self.success else 'FAILED'
        return f"ActionResult({status}, output={self.output[:50]}...)" if len(self.output) > 50 else f"ActionResult({status}, output={self.output})"


class ActionRegistry:
    """
    Action 注册和执行
    
    管理所有可用的 Action 类型，提供注册和执行功能。
    内置常用的 Kubernetes、HTTP、脚本等 Action。
    """
    
    def __init__(self):
        self._actions: Dict[str, Callable] = {}
        self._register_builtin_actions()
    
    def _register_builtin_actions(self):
        """注册所有内置 Action"""
        self.register('kubectl_scale', self._kubectl_scale)
        self.register('kubectl_restart', self._kubectl_restart)
        self.register('kubectl_logs', self._kubectl_logs)
        self.register('http_request', self._http_request)
        self.register('script', self._script)
        self.register('wait', self._wait)
        self.register('verify_alert_resolved', self._verify_alert_resolved)
        self.register('notify', self._notify)
    
    def register(self, name: str, handler: Callable):
        """
        注册自定义 Action
        
        Args:
            name: Action 名称
            handler: 处理函数，接收 (params: dict) 返回 ActionResult
        """
        self._actions[name] = handler
        logger.debug(f"已注册 Action: {name}")
    
    def list_actions(self) -> list:
        """列出所有已注册的 Action 名称"""
        return list(self._actions.keys())
    
    def execute(self, action_name: str, params: Dict[str, Any], dry_run: bool = False) -> ActionResult:
        """
        执行指定 Action
        
        Args:
            action_name: Action 名称
            params: 动作参数
            dry_run: 是否为干运行模式（只记录不执行）
            
        Returns:
            ActionResult: 执行结果
        """
        if action_name not in self._actions:
            logger.error(f"未知的 Action: {action_name}")
            return ActionResult(False, error=f'Unknown action: {action_name}')
        
        if dry_run:
            logger.info(f"[DRY RUN] Would execute: {action_name} with params: {params}")
            return ActionResult(True, output=f'[DRY RUN] Would execute: {action_name} with params: {params}')
        
        try:
            logger.info(f"执行 Action: {action_name}, params: {params}")
            result = self._actions[action_name](params)
            if result.success:
                logger.info(f"Action {action_name} 执行成功: {result.output[:100]}...")
            else:
                logger.warning(f"Action {action_name} 执行失败: {result.error}")
            return result
        except Exception as e:
            logger.error(f"Action {action_name} 执行异常: {e}", exc_info=True)
            return ActionResult(False, error=str(e))
    
    def _kubectl_scale(self, params: Dict[str, Any]) -> ActionResult:
        """
        K8s 副本扩缩容
        
        支持绝对数量和增量（+N/-N）两种方式。
        
        Params:
            deployment: Deployment 名称
            namespace: 命名空间，默认 'default'
            replicas: 副本数量，支持 +N/-N 格式表示增减
        """
        deployment = params.get('deployment')
        namespace = params.get('namespace', 'default')
        replicas = params.get('replicas')
        
        if not deployment:
            return ActionResult(False, error="缺少 deployment 参数")
        if replicas is None:
            return ActionResult(False, error="缺少 replicas 参数")
        
        replicas_str = str(replicas)
        
        # 处理增量格式（+N 或 -N）
        if replicas_str.startswith('+') or replicas_str.startswith('-'):
            try:
                # 先获取当前副本数
                get_cmd = f"kubectl get deployment/{deployment} -n {namespace} -o jsonpath='{{.spec.replicas}}'"
                get_result = subprocess.run(
                    get_cmd, shell=True, capture_output=True, text=True, timeout=30
                )
                if get_result.returncode != 0:
                    return ActionResult(False, error=f"获取当前副本数失败: {get_result.stderr}")
                
                current_replicas = int(get_result.stdout.strip().strip("'"))
                delta = int(replicas_str)
                target_replicas = max(0, current_replicas + delta)  # 确保不会小于0
                
                logger.info(f"副本数变更: {current_replicas} -> {target_replicas} (delta={delta})")
                replicas_str = str(target_replicas)
            except (ValueError, subprocess.TimeoutExpired) as e:
                return ActionResult(False, error=f"计算目标副本数失败: {e}")
        
        # 执行扩缩容
        cmd = f"kubectl scale deployment/{deployment} --replicas={replicas_str} -n {namespace}"
        logger.info(f"执行命令: {cmd}")
        
        try:
            result = subprocess.run(
                cmd.split(), capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                return ActionResult(True, output=result.stdout.strip() or f"deployment/{deployment} scaled to {replicas_str} replicas")
            return ActionResult(False, error=result.stderr.strip())
        except subprocess.TimeoutExpired:
            return ActionResult(False, error="命令执行超时")
        except Exception as e:
            return ActionResult(False, error=str(e))
    
    def _kubectl_restart(self, params: Dict[str, Any]) -> ActionResult:
        """
        重启 Deployment（滚动重启）
        
        Params:
            deployment: Deployment 名称
            namespace: 命名空间，默认 'default'
        """
        deployment = params.get('deployment')
        namespace = params.get('namespace', 'default')
        
        if not deployment:
            return ActionResult(False, error="缺少 deployment 参数")
        
        cmd = f"kubectl rollout restart deployment/{deployment} -n {namespace}"
        logger.info(f"执行命令: {cmd}")
        
        try:
            result = subprocess.run(
                cmd.split(), capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                return ActionResult(True, output=result.stdout.strip() or f"deployment/{deployment} restarted")
            return ActionResult(False, error=result.stderr.strip())
        except subprocess.TimeoutExpired:
            return ActionResult(False, error="命令执行超时")
        except Exception as e:
            return ActionResult(False, error=str(e))
    
    def _kubectl_logs(self, params: Dict[str, Any]) -> ActionResult:
        """
        获取 Pod 日志
        
        Params:
            pod: Pod 名称（支持 label selector）
            namespace: 命名空间，默认 'default'
            tail: 获取最后 N 行，默认 100
            container: 容器名（多容器 Pod 时需要指定）
        """
        pod = params.get('pod')
        namespace = params.get('namespace', 'default')
        tail = params.get('tail', 100)
        container = params.get('container')
        
        if not pod:
            return ActionResult(False, error="缺少 pod 参数")
        
        cmd = f"kubectl logs {pod} -n {namespace} --tail={tail}"
        if container:
            cmd += f" -c {container}"
        
        logger.info(f"执行命令: {cmd}")
        
        try:
            result = subprocess.run(
                cmd.split(), capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                return ActionResult(True, output=result.stdout)
            return ActionResult(False, error=result.stderr.strip())
        except subprocess.TimeoutExpired:
            return ActionResult(False, error="命令执行超时")
        except Exception as e:
            return ActionResult(False, error=str(e))
    
    def _http_request(self, params: Dict[str, Any]) -> ActionResult:
        """
        发送 HTTP 请求
        
        Params:
            url: 请求 URL
            method: HTTP 方法，默认 'POST'
            headers: 请求头字典
            body: 请求体（JSON）
            timeout: 超时秒数，默认 30
        """
        url = params.get('url')
        method = params.get('method', 'POST').upper()
        headers = params.get('headers', {})
        body = params.get('body', {})
        timeout = params.get('timeout', 30)
        
        if not url:
            return ActionResult(False, error="缺少 url 参数")
        
        logger.info(f"发送 HTTP 请求: {method} {url}")
        
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=body if method in ('POST', 'PUT', 'PATCH') else None,
                params=body if method == 'GET' else None,
                timeout=timeout
            )
            
            if 200 <= response.status_code < 300:
                return ActionResult(True, output=f"HTTP {response.status_code}: {response.text[:500]}")
            return ActionResult(False, error=f"HTTP {response.status_code}: {response.text[:500]}")
        except requests.exceptions.Timeout:
            return ActionResult(False, error="HTTP 请求超时")
        except requests.exceptions.ConnectionError:
            return ActionResult(False, error="无法连接到目标服务器")
        except Exception as e:
            return ActionResult(False, error=str(e))
    
    def _script(self, params: Dict[str, Any]) -> ActionResult:
        """
        执行脚本命令
        
        注意：此 Action 会进行安全检查，禁止执行危险命令。
        
        Params:
            command: 要执行的命令
            timeout: 超时秒数，默认 60
            shell: 是否使用 shell 执行，默认 True
        """
        command = params.get('command')
        timeout = params.get('timeout', 60)
        use_shell = params.get('shell', True)
        
        if not command:
            return ActionResult(False, error="缺少 command 参数")
        
        # 安全检查
        command_lower = command.lower()
        for dangerous in DANGEROUS_COMMANDS:
            if dangerous.lower() in command_lower:
                logger.error(f"检测到危险命令被拦截: {command}")
                return ActionResult(False, error=f"安全检查失败: 命令包含危险操作 '{dangerous}'")
        
        # 额外的正则检查
        if re.search(r'rm\s+(-[rf]+\s+)*/', command_lower):
            return ActionResult(False, error="安全检查失败: 禁止删除根目录相关文件")
        
        logger.info(f"执行脚本命令: {command}")
        
        try:
            if use_shell:
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True, timeout=timeout
                )
            else:
                result = subprocess.run(
                    command.split(), capture_output=True, text=True, timeout=timeout
                )
            
            if result.returncode == 0:
                return ActionResult(True, output=result.stdout.strip())
            return ActionResult(False, error=result.stderr.strip() or f"命令退出码: {result.returncode}")
        except subprocess.TimeoutExpired:
            return ActionResult(False, error=f"命令执行超时 (>{timeout}秒)")
        except Exception as e:
            return ActionResult(False, error=str(e))
    
    def _wait(self, params: Dict[str, Any]) -> ActionResult:
        """
        等待指定时间
        
        Params:
            duration: 等待秒数，默认 60
        """
        duration = params.get('duration', 60)
        
        try:
            duration = int(duration)
            if duration < 0:
                duration = 0
            if duration > 3600:  # 最大等待1小时
                duration = 3600
                logger.warning("等待时间超过1小时，已限制为3600秒")
        except (ValueError, TypeError):
            return ActionResult(False, error=f"无效的 duration 值: {duration}")
        
        logger.info(f"等待 {duration} 秒...")
        time.sleep(duration)
        return ActionResult(True, output=f"等待 {duration} 秒完成")
    
    def _verify_alert_resolved(self, params: Dict[str, Any]) -> ActionResult:
        """
        验证告警是否已解除
        
        循环检查告警状态，直到告警消失或超时。
        
        Params:
            alert_hash: 告警哈希值
            timeout: 超时秒数，默认 300
            check_interval: 检查间隔秒数，默认 30
        """
        alert_hash = params.get('alert_hash')
        timeout = params.get('timeout', 300)
        check_interval = params.get('check_interval', 30)
        
        if not alert_hash:
            # 如果没有提供 alert_hash，只等待一段时间后返回成功
            logger.info("未提供 alert_hash，跳过告警状态检查")
            return ActionResult(True, output="跳过告警验证（未提供 alert_hash）")
        
        start_time = time.time()
        check_count = 0
        
        while time.time() - start_time < timeout:
            check_count += 1
            logger.info(f"检查告警状态 #{check_count}: hash={alert_hash[:16]}...")
            
            try:
                # 查询最近是否有同哈希的新告警
                from core.models import WebhookEvent, get_session
                from datetime import datetime, timedelta
                
                session = get_session()
                try:
                    # 检查最近一段时间内是否有新的同类告警
                    recent_threshold = datetime.now() - timedelta(seconds=check_interval * 2)
                    recent_alert = session.query(WebhookEvent).filter(
                        WebhookEvent.alert_hash == alert_hash,
                        WebhookEvent.timestamp >= recent_threshold
                    ).first()
                    
                    if recent_alert is None:
                        logger.info(f"告警已解除: hash={alert_hash[:16]}...")
                        return ActionResult(True, output=f"告警已解除，共检查 {check_count} 次")
                finally:
                    session.close()
                    
            except Exception as e:
                logger.warning(f"检查告警状态失败: {e}")
            
            logger.info(f"告警仍在持续，等待 {check_interval} 秒后重试...")
            time.sleep(check_interval)
        
        elapsed = int(time.time() - start_time)
        return ActionResult(False, error=f"告警验证超时: {elapsed}秒内告警未解除，共检查 {check_count} 次")
    
    def _notify(self, params: Dict[str, Any]) -> ActionResult:
        """
        发送通知
        
        目前仅记录日志，后续可扩展为实际通知渠道。
        
        Params:
            message: 通知消息
            channel: 通知渠道（预留）
        """
        message = params.get('message', '')
        channel = params.get('channel', 'log')
        
        if not message:
            return ActionResult(False, error="缺少 message 参数")
        
        logger.info(f"[Remediation 通知] [{channel}] {message}")
        
        # 可以在这里扩展实际的通知逻辑
        # 例如发送到飞书、钉钉、邮件等
        
        return ActionResult(True, output=f"通知已发送: {message}")
