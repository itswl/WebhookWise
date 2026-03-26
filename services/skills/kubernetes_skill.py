"""Kubernetes Skill - K8s 集群连接器

通过 subprocess 调用 kubectl 命令与 Kubernetes 集群交互，
提供 Pod 状态查询、日志获取、资源使用监控等能力。
"""

import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

from core.config import Config
from services.skills.base import SkillBase

logger = logging.getLogger(__name__)


class KubernetesSkill(SkillBase):
    """Kubernetes 平台连接器"""

    name = "kubernetes"
    description = "Kubernetes 集群连接器，提供 Pod 状态查询、日志获取、资源监控等能力"
    enabled = Config.SKILL_K8S_ENABLED
    is_builtin = True

    def __init__(self):
        self.kubeconfig = Config.SKILL_K8S_KUBECONFIG
        self.context = Config.SKILL_K8S_CONTEXT
        self.timeout = 30
        self.config = {
            "kubeconfig": self.kubeconfig,
            "context": self.context,
            "timeout": self.timeout
        }

    def update_config(self, config: Dict[str, Any]) -> bool:
        """更新 Kubernetes Skill 配置"""
        try:
            if "kubeconfig" in config:
                self.kubeconfig = config["kubeconfig"]
                self.config["kubeconfig"] = config["kubeconfig"]
            if "context" in config:
                self.context = config["context"]
                self.config["context"] = config["context"]
            if "timeout" in config:
                self.timeout = int(config["timeout"])
                self.config["timeout"] = self.timeout
            logger.info(f"Kubernetes Skill 配置已更新")
            return True
        except Exception as e:
            logger.error(f"更新 Kubernetes Skill 配置失败: {e}")
            return False
    
    def _build_kubectl_cmd(self, base_cmd: str) -> List[str]:
        """构建 kubectl 命令，添加 kubeconfig 和 context 参数"""
        cmd = ["kubectl"]
        
        if self.kubeconfig:
            cmd.extend(["--kubeconfig", self.kubeconfig])
        
        if self.context:
            cmd.extend(["--context", self.context])
        
        cmd.extend(base_cmd.split())
        return cmd
    
    def _run_kubectl(self, cmd: List[str], timeout: int = None) -> Dict[str, Any]:
        """执行 kubectl 命令并返回结果"""
        timeout = timeout or self.timeout
        
        try:
            logger.debug(f"执行 kubectl 命令: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            if result.returncode == 0:
                return {"success": True, "output": result.stdout, "error": None}
            else:
                return {"success": False, "output": None, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            logger.error(f"kubectl 命令执行超时 (>{timeout}s)")
            return {"success": False, "output": None, "error": f"命令执行超时 (>{timeout}s)"}
        except FileNotFoundError:
            logger.error("kubectl 命令未找到，请确保 kubectl 已安装")
            return {"success": False, "output": None, "error": "kubectl 命令未找到，请确保 kubectl 已安装"}
        except Exception as e:
            logger.error(f"kubectl 命令执行异常: {e}")
            return {"success": False, "output": None, "error": str(e)}
    
    def get_capabilities(self) -> List[dict]:
        """返回该 Skill 支持的所有操作"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "kubernetes__get_pod_status",
                    "description": "查询 Pod 的运行状态，支持按名称或标签选择器查询",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes 命名空间，默认为 'default'"
                            },
                            "pod_name": {
                                "type": "string",
                                "description": "Pod 名称（可选，与 label_selector 二选一）"
                            },
                            "label_selector": {
                                "type": "string",
                                "description": "标签选择器，如 'app=nginx'（可选，与 pod_name 二选一）"
                            }
                        },
                        "required": ["namespace"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "kubernetes__get_pod_logs",
                    "description": "获取 Pod 的容器日志",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes 命名空间"
                            },
                            "pod_name": {
                                "type": "string",
                                "description": "Pod 名称"
                            },
                            "tail_lines": {
                                "type": "integer",
                                "description": "获取最后 N 行日志，默认 100",
                                "default": 100
                            },
                            "container": {
                                "type": "string",
                                "description": "容器名称（多容器 Pod 时需要指定）"
                            }
                        },
                        "required": ["namespace", "pod_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "kubernetes__get_pod_events",
                    "description": "获取与 Pod 相关的 Kubernetes Events",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes 命名空间"
                            },
                            "pod_name": {
                                "type": "string",
                                "description": "Pod 名称（可选，不提供则返回命名空间内所有事件）"
                            }
                        },
                        "required": ["namespace"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "kubernetes__get_resource_usage",
                    "description": "获取资源使用率（CPU/内存），需要 metrics-server",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes 命名空间，默认为 'default'"
                            },
                            "resource_type": {
                                "type": "string",
                                "description": "资源类型：pod、node，默认为 'pod'",
                                "enum": ["pod", "node"],
                                "default": "pod"
                            }
                        },
                        "required": ["namespace"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "kubernetes__get_deployment_status",
                    "description": "查询 Deployment 的状态信息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes 命名空间，默认为 'default'"
                            },
                            "deployment_name": {
                                "type": "string",
                                "description": "Deployment 名称（可选，不提供则返回所有 Deployment）"
                            }
                        },
                        "required": ["namespace"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "kubernetes__describe_resource",
                    "description": "使用 kubectl describe 查看资源详细信息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes 命名空间，默认为 'default'"
                            },
                            "resource_type": {
                                "type": "string",
                                "description": "资源类型：pod、deployment、service、node、event 等"
                            },
                            "resource_name": {
                                "type": "string",
                                "description": "资源名称"
                            }
                        },
                        "required": ["namespace", "resource_type", "resource_name"]
                    }
                }
            }
        ]
    
    def execute(self, action: str, params: dict) -> dict:
        """执行具体操作"""
        if not self.enabled:
            return {"success": False, "data": None, "error": "Kubernetes skill is disabled"}
        
        logger.info(f"执行 Kubernetes skill: {action}, params: {params}")
        
        try:
            if action == "get_pod_status":
                result = self._get_pod_status(params)
            elif action == "get_pod_logs":
                result = self._get_pod_logs(params)
            elif action == "get_pod_events":
                result = self._get_pod_events(params)
            elif action == "get_resource_usage":
                result = self._get_resource_usage(params)
            elif action == "get_deployment_status":
                result = self._get_deployment_status(params)
            elif action == "describe_resource":
                result = self._describe_resource(params)
            else:
                return {"success": False, "data": None, "error": f"Unknown action: {action}"}
            
            if result.get("success"):
                logger.info(f"Kubernetes skill {action} 执行成功")
                return {"success": True, "data": result.get("output"), "error": None}
            else:
                logger.warning(f"Kubernetes skill {action} 执行失败: {result.get('error')}")
                return {"success": False, "data": None, "error": result.get("error")}
        
        except Exception as e:
            logger.error(f"Kubernetes skill {action} 执行异常: {e}", exc_info=True)
            return {"success": False, "data": None, "error": str(e)}
    
    def _get_pod_status(self, params: dict) -> Dict[str, Any]:
        """查询 Pod 状态"""
        namespace = params.get("namespace", "default")
        pod_name = params.get("pod_name")
        label_selector = params.get("label_selector")
        
        cmd = f"get pods -n {namespace}"
        
        if pod_name:
            cmd += f" {pod_name}"
        if label_selector:
            cmd += f" -l {label_selector}"
        
        cmd += " -o json"
        
        kubectl_cmd = self._build_kubectl_cmd(cmd)
        return self._run_kubectl(kubectl_cmd)
    
    def _get_pod_logs(self, params: dict) -> Dict[str, Any]:
        """获取 Pod 日志"""
        namespace = params.get("namespace")
        pod_name = params.get("pod_name")
        tail_lines = params.get("tail_lines", 100)
        container = params.get("container")
        
        cmd = f"logs {pod_name} -n {namespace} --tail={tail_lines}"
        
        if container:
            cmd += f" -c {container}"
        
        kubectl_cmd = self._build_kubectl_cmd(cmd)
        return self._run_kubectl(kubectl_cmd)
    
    def _get_pod_events(self, params: dict) -> Dict[str, Any]:
        """获取 Pod 相关 Events"""
        namespace = params.get("namespace")
        pod_name = params.get("pod_name")
        
        cmd = f"get events -n {namespace}"
        
        if pod_name:
            cmd += f" --field-selector involvedObject.name={pod_name}"
        
        cmd += " --sort-by='.lastTimestamp' -o json"
        
        kubectl_cmd = self._build_kubectl_cmd(cmd)
        return self._run_kubectl(kubectl_cmd)
    
    def _get_resource_usage(self, params: dict) -> Dict[str, Any]:
        """获取资源使用率（kubectl top）"""
        namespace = params.get("namespace", "default")
        resource_type = params.get("resource_type", "pod")
        
        cmd = f"top {resource_type} -n {namespace}"
        
        kubectl_cmd = self._build_kubectl_cmd(cmd)
        return self._run_kubectl(kubectl_cmd)
    
    def _get_deployment_status(self, params: dict) -> Dict[str, Any]:
        """查询 Deployment 状态"""
        namespace = params.get("namespace", "default")
        deployment_name = params.get("deployment_name")
        
        cmd = f"get deployments -n {namespace}"
        
        if deployment_name:
            cmd += f" {deployment_name}"
        
        cmd += " -o json"
        
        kubectl_cmd = self._build_kubectl_cmd(cmd)
        return self._run_kubectl(kubectl_cmd)
    
    def _describe_resource(self, params: dict) -> Dict[str, Any]:
        """使用 kubectl describe 查看资源详情"""
        namespace = params.get("namespace", "default")
        resource_type = params.get("resource_type")
        resource_name = params.get("resource_name")
        
        cmd = f"describe {resource_type} {resource_name} -n {namespace}"
        
        kubectl_cmd = self._build_kubectl_cmd(cmd)
        return self._run_kubectl(kubectl_cmd)
    
    def health_check(self) -> dict:
        """检查 Kubernetes 连接是否可用"""
        if not self.enabled:
            return {"healthy": False, "message": "Kubernetes skill is disabled", "details": {}}
        
        try:
            cmd = self._build_kubectl_cmd("cluster-info")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )
            
            if result.returncode == 0:
                return {
                    "healthy": True,
                    "message": "Kubernetes cluster connection is healthy",
                    "details": {"cluster_info": result.stdout.strip()[:200]}
                }
            else:
                return {
                    "healthy": False,
                    "message": "Failed to connect to Kubernetes cluster",
                    "details": {"error": result.stderr.strip()}
                }
        except Exception as e:
            logger.error(f"Kubernetes health check failed: {e}")
            return {
                "healthy": False,
                "message": f"Health check failed: {str(e)}",
                "details": {}
            }


# 全局实例（auto_discover 会扫描到）
kubernetes_skill = KubernetesSkill()
