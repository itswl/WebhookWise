"""Grafana Skill - Grafana 可视化平台连接器

通过 HTTP API 与 Grafana 交互，提供 Dashboard 查询、
注解获取、告警历史查询等能力。
"""

import logging
from typing import Any, Dict, List

import requests

from core.config import Config
from services.skills.base import SkillBase

logger = logging.getLogger(__name__)


class GrafanaSkill(SkillBase):
    """Grafana 平台连接器"""

    name = "grafana"
    description = "Grafana 可视化平台连接器，提供 Dashboard 查询、注解获取、告警历史查询等能力"
    enabled = Config.SKILL_GRAFANA_ENABLED
    is_builtin = True

    def __init__(self):
        self.base_url = Config.SKILL_GRAFANA_URL.rstrip('/')
        self.api_token = Config.SKILL_GRAFANA_API_TOKEN
        self.timeout = 15
        self.config = {
            "url": self.base_url,
            "api_token": self.api_token,
            "timeout": self.timeout
        }

    def update_config(self, config: Dict[str, Any]) -> bool:
        """更新 Grafana Skill 配置"""
        try:
            if "url" in config:
                self.base_url = config["url"].rstrip('/')
                self.config["url"] = self.base_url
            if "api_token" in config:
                self.api_token = config["api_token"]
                self.config["api_token"] = config["api_token"]
            if "timeout" in config:
                self.timeout = int(config["timeout"])
                self.config["timeout"] = self.timeout
            logger.info(f"Grafana Skill 配置已更新")
            return True
        except Exception as e:
            logger.error(f"更新 Grafana Skill 配置失败: {e}")
            return False
    
    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers
    
    def _make_request(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """发送 HTTP GET 请求到 Grafana API"""
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers()
        
        try:
            logger.debug(f"Grafana API 请求: GET {url}, params: {params}")
            
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
            
            response.raise_for_status()
            return {"success": True, "data": response.json(), "error": None}
        
        except requests.exceptions.Timeout:
            logger.error(f"Grafana API 请求超时 (>{self.timeout}s)")
            return {"success": False, "data": None, "error": f"请求超时 (>{self.timeout}s)"}
        except requests.exceptions.ConnectionError:
            logger.error(f"无法连接到 Grafana: {self.base_url}")
            return {"success": False, "data": None, "error": f"无法连接到 Grafana: {self.base_url}"}
        except requests.exceptions.HTTPError as e:
            logger.error(f"Grafana API HTTP 错误: {e}")
            return {"success": False, "data": None, "error": f"HTTP 错误: {e.response.status_code} - {e.response.text[:200]}"}
        except Exception as e:
            logger.error(f"Grafana API 请求异常: {e}")
            return {"success": False, "data": None, "error": str(e)}
    
    def get_capabilities(self) -> List[dict]:
        """返回该 Skill 支持的所有操作"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "grafana__get_dashboard_panels",
                    "description": "获取指定 Dashboard 的面板数据和配置信息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "dashboard_uid": {
                                "type": "string",
                                "description": "Dashboard 的 UID，如 'node-exporter'"
                            }
                        },
                        "required": ["dashboard_uid"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "grafana__get_annotations",
                    "description": "获取 Grafana 注解（Annotations），可用于查看事件标记",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "from_time": {
                                "type": "string",
                                "description": "开始时间（Unix 时间戳毫秒或 RFC3339 格式）"
                            },
                            "to_time": {
                                "type": "string",
                                "description": "结束时间（Unix 时间戳毫秒或 RFC3339 格式）"
                            },
                            "dashboard_id": {
                                "type": "integer",
                                "description": "Dashboard ID 过滤（可选）"
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "标签过滤列表（可选）"
                            }
                        },
                        "required": ["from_time", "to_time"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "grafana__get_alert_history",
                    "description": "获取告警规则历史（通过 Provisioning API）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "返回结果数量限制，默认 50",
                                "default": 50
                            }
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "grafana__search_dashboards",
                    "description": "搜索 Dashboard，支持按名称或标签搜索",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索关键词（可选）"
                            },
                            "tag": {
                                "type": "string",
                                "description": "标签过滤（可选）"
                            }
                        }
                    }
                }
            }
        ]
    
    def execute(self, action: str, params: dict) -> dict:
        """执行具体操作"""
        if not self.enabled:
            return {"success": False, "data": None, "error": "Grafana skill is disabled"}
        
        logger.info(f"执行 Grafana skill: {action}, params: {params}")
        
        try:
            if action == "get_dashboard_panels":
                result = self._get_dashboard_panels(params)
            elif action == "get_annotations":
                result = self._get_annotations(params)
            elif action == "get_alert_history":
                result = self._get_alert_history(params)
            elif action == "search_dashboards":
                result = self._search_dashboards(params)
            else:
                return {"success": False, "data": None, "error": f"Unknown action: {action}"}
            
            if result.get("success"):
                logger.info(f"Grafana skill {action} 执行成功")
                return {"success": True, "data": result.get("data"), "error": None}
            else:
                logger.warning(f"Grafana skill {action} 执行失败: {result.get('error')}")
                return {"success": False, "data": None, "error": result.get("error")}
        
        except Exception as e:
            logger.error(f"Grafana skill {action} 执行异常: {e}", exc_info=True)
            return {"success": False, "data": None, "error": str(e)}
    
    def _get_dashboard_panels(self, params: dict) -> Dict[str, Any]:
        """获取 Dashboard 面板数据"""
        dashboard_uid = params.get("dashboard_uid")
        
        if not dashboard_uid:
            return {"success": False, "data": None, "error": "dashboard_uid 参数不能为空"}
        
        return self._make_request(f"/api/dashboards/uid/{dashboard_uid}")
    
    def _get_annotations(self, params: dict) -> Dict[str, Any]:
        """获取注解"""
        from_time = params.get("from_time")
        to_time = params.get("to_time")
        dashboard_id = params.get("dashboard_id")
        tags = params.get("tags")
        
        if not from_time:
            return {"success": False, "data": None, "error": "from_time 参数不能为空"}
        if not to_time:
            return {"success": False, "data": None, "error": "to_time 参数不能为空"}
        
        request_params = {
            "from": from_time,
            "to": to_time
        }
        
        if dashboard_id:
            request_params["dashboardId"] = dashboard_id
        if tags:
            request_params["tags"] = tags
        
        return self._make_request("/api/annotations", request_params)
    
    def _get_alert_history(self, params: dict) -> Dict[str, Any]:
        """获取告警历史"""
        limit = params.get("limit", 50)
        
        request_params = {"limit": limit}
        return self._make_request("/api/v1/provisioning/alert-rules", request_params)
    
    def _search_dashboards(self, params: dict) -> Dict[str, Any]:
        """搜索 Dashboard"""
        query = params.get("query")
        tag = params.get("tag")
        
        request_params = {}
        if query:
            request_params["query"] = query
        if tag:
            request_params["tag"] = tag
        
        return self._make_request("/api/search", request_params)
    
    def health_check(self) -> dict:
        """检查 Grafana 连接是否可用"""
        if not self.enabled:
            return {"healthy": False, "message": "Grafana skill is disabled", "details": {}}
        
        try:
            url = f"{self.base_url}/api/health"
            headers = self._get_headers()
            
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            
            return {
                "healthy": True,
                "message": "Grafana connection is healthy",
                "details": {
                    "version": data.get("version", "unknown"),
                    "database": data.get("database", "unknown"),
                    "url": self.base_url
                }
            }
        
        except requests.exceptions.ConnectionError:
            return {
                "healthy": False,
                "message": f"Cannot connect to Grafana at {self.base_url}",
                "details": {}
            }
        except Exception as e:
            logger.error(f"Grafana health check failed: {e}")
            return {
                "healthy": False,
                "message": f"Health check failed: {str(e)}",
                "details": {}
            }


# 全局实例（auto_discover 会扫描到）
grafana_skill = GrafanaSkill()
