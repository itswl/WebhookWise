"""Prometheus Skill - Prometheus 监控平台连接器

通过 HTTP API 与 Prometheus 交互，提供 PromQL 查询、
告警规则获取、活跃告警查询等能力。
"""

import logging
from typing import Any, Dict, List

import requests

from core.config import Config
from services.skills.base import SkillBase

logger = logging.getLogger(__name__)


class PrometheusSkill(SkillBase):
    """Prometheus 平台连接器"""

    name = "prometheus"
    description = "Prometheus 监控平台连接器，提供 PromQL 查询、告警规则获取、活跃告警查询等能力"
    enabled = Config.SKILL_PROMETHEUS_ENABLED
    is_builtin = True

    def __init__(self):
        self.base_url = Config.SKILL_PROMETHEUS_URL.rstrip('/')
        self.auth_token = Config.SKILL_PROMETHEUS_AUTH_TOKEN
        self.timeout = 15
        self.config = {
            "url": self.base_url,
            "auth_token": self.auth_token,
            "timeout": self.timeout
        }

    def update_config(self, config: Dict[str, Any]) -> bool:
        """更新 Prometheus Skill 配置"""
        try:
            if "url" in config:
                self.base_url = config["url"].rstrip('/')
                self.config["url"] = self.base_url
            if "auth_token" in config:
                self.auth_token = config["auth_token"]
                self.config["auth_token"] = config["auth_token"]
            if "timeout" in config:
                self.timeout = int(config["timeout"])
                self.config["timeout"] = self.timeout
            logger.info(f"Prometheus Skill 配置已更新")
            return True
        except Exception as e:
            logger.error(f"更新 Prometheus Skill 配置失败: {e}")
            return False
    
    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers
    
    def _make_request(self, endpoint: str, params: Dict[str, Any] = None, method: str = "GET") -> Dict[str, Any]:
        """发送 HTTP 请求到 Prometheus API"""
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers()
        
        try:
            logger.debug(f"Prometheus API 请求: {method} {url}, params: {params}")
            
            if method == "GET":
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout
                )
            else:
                response = requests.post(
                    url,
                    headers=headers,
                    data=params,
                    timeout=self.timeout
                )
            
            response.raise_for_status()
            return {"success": True, "data": response.json(), "error": None}
        
        except requests.exceptions.Timeout:
            logger.error(f"Prometheus API 请求超时 (>{self.timeout}s)")
            return {"success": False, "data": None, "error": f"请求超时 (>{self.timeout}s)"}
        except requests.exceptions.ConnectionError:
            logger.error(f"无法连接到 Prometheus: {self.base_url}")
            return {"success": False, "data": None, "error": f"无法连接到 Prometheus: {self.base_url}"}
        except requests.exceptions.HTTPError as e:
            logger.error(f"Prometheus API HTTP 错误: {e}")
            return {"success": False, "data": None, "error": f"HTTP 错误: {e.response.status_code} - {e.response.text[:200]}"}
        except Exception as e:
            logger.error(f"Prometheus API 请求异常: {e}")
            return {"success": False, "data": None, "error": str(e)}
    
    def get_capabilities(self) -> List[dict]:
        """返回该 Skill 支持的所有操作"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "prometheus__query_instant",
                    "description": "执行即时 PromQL 查询，获取当前指标值",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "PromQL 查询语句，如 'up', 'cpu_usage{job=\"node\"}'"
                            },
                            "time": {
                                "type": "string",
                                "description": "查询时间点（Unix 时间戳或 RFC3339 格式），可选"
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "prometheus__query_range",
                    "description": "执行范围 PromQL 查询，获取一段时间内的指标数据",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "PromQL 查询语句"
                            },
                            "start": {
                                "type": "string",
                                "description": "开始时间（Unix 时间戳或 RFC3339 格式）"
                            },
                            "end": {
                                "type": "string",
                                "description": "结束时间（Unix 时间戳或 RFC3339 格式）"
                            },
                            "step": {
                                "type": "string",
                                "description": "查询步长，如 '60s', '5m', '1h'，默认 '60s'",
                                "default": "60s"
                            }
                        },
                        "required": ["query", "start", "end"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "prometheus__get_alert_rules",
                    "description": "获取 Prometheus 告警规则列表",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "group": {
                                "type": "string",
                                "description": "规则组名称过滤（可选）"
                            }
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "prometheus__get_active_alerts",
                    "description": "获取当前活跃的告警列表",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "prometheus__get_metric_metadata",
                    "description": "获取指标的元数据信息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "metric_name": {
                                "type": "string",
                                "description": "指标名称，如 'http_requests_total'"
                            }
                        },
                        "required": ["metric_name"]
                    }
                }
            }
        ]
    
    def execute(self, action: str, params: dict) -> dict:
        """执行具体操作"""
        if not self.enabled:
            return {"success": False, "data": None, "error": "Prometheus skill is disabled"}
        
        logger.info(f"执行 Prometheus skill: {action}, params: {params}")
        
        try:
            if action == "query_instant":
                result = self._query_instant(params)
            elif action == "query_range":
                result = self._query_range(params)
            elif action == "get_alert_rules":
                result = self._get_alert_rules(params)
            elif action == "get_active_alerts":
                result = self._get_active_alerts(params)
            elif action == "get_metric_metadata":
                result = self._get_metric_metadata(params)
            else:
                return {"success": False, "data": None, "error": f"Unknown action: {action}"}
            
            if result.get("success"):
                logger.info(f"Prometheus skill {action} 执行成功")
                return {"success": True, "data": result.get("data"), "error": None}
            else:
                logger.warning(f"Prometheus skill {action} 执行失败: {result.get('error')}")
                return {"success": False, "data": None, "error": result.get("error")}
        
        except Exception as e:
            logger.error(f"Prometheus skill {action} 执行异常: {e}", exc_info=True)
            return {"success": False, "data": None, "error": str(e)}
    
    def _query_instant(self, params: dict) -> Dict[str, Any]:
        """即时 PromQL 查询"""
        query = params.get("query")
        time = params.get("time")
        
        if not query:
            return {"success": False, "data": None, "error": "query 参数不能为空"}
        
        request_params = {"query": query}
        if time:
            request_params["time"] = time
        
        return self._make_request("/api/v1/query", request_params)
    
    def _query_range(self, params: dict) -> Dict[str, Any]:
        """范围 PromQL 查询"""
        query = params.get("query")
        start = params.get("start")
        end = params.get("end")
        step = params.get("step", "60s")
        
        if not query:
            return {"success": False, "data": None, "error": "query 参数不能为空"}
        if not start:
            return {"success": False, "data": None, "error": "start 参数不能为空"}
        if not end:
            return {"success": False, "data": None, "error": "end 参数不能为空"}
        
        request_params = {
            "query": query,
            "start": start,
            "end": end,
            "step": step
        }
        
        return self._make_request("/api/v1/query_range", request_params)
    
    def _get_alert_rules(self, params: dict) -> Dict[str, Any]:
        """获取告警规则"""
        group = params.get("group")
        
        request_params = {}
        if group:
            request_params["group"] = group
        
        return self._make_request("/api/v1/rules", request_params)
    
    def _get_active_alerts(self, params: dict) -> Dict[str, Any]:
        """获取活跃告警"""
        return self._make_request("/api/v1/alerts")
    
    def _get_metric_metadata(self, params: dict) -> Dict[str, Any]:
        """获取指标元数据"""
        metric_name = params.get("metric_name")
        
        if not metric_name:
            return {"success": False, "data": None, "error": "metric_name 参数不能为空"}
        
        request_params = {"metric": metric_name}
        return self._make_request("/api/v1/metadata", request_params)
    
    def health_check(self) -> dict:
        """检查 Prometheus 连接是否可用"""
        if not self.enabled:
            return {"healthy": False, "message": "Prometheus skill is disabled", "details": {}}
        
        try:
            url = f"{self.base_url}/api/v1/status/buildinfo"
            headers = self._get_headers()
            
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") == "success":
                return {
                    "healthy": True,
                    "message": "Prometheus connection is healthy",
                    "details": {
                        "version": data.get("data", {}).get("version", "unknown"),
                        "url": self.base_url
                    }
                }
            else:
                return {
                    "healthy": False,
                    "message": "Prometheus returned non-success status",
                    "details": {"response": data}
                }
        
        except requests.exceptions.ConnectionError:
            return {
                "healthy": False,
                "message": f"Cannot connect to Prometheus at {self.base_url}",
                "details": {}
            }
        except Exception as e:
            logger.error(f"Prometheus health check failed: {e}")
            return {
                "healthy": False,
                "message": f"Health check failed: {str(e)}",
                "details": {}
            }


# 全局实例（auto_discover 会扫描到）
prometheus_skill = PrometheusSkill()
