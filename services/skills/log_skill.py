"""Log Skill - 日志平台连接器

支持 Elasticsearch 和 Loki 两种后端，提供日志搜索、
错误上下文获取、日志统计、Trace ID 搜索等能力。
"""

import logging
from typing import Any, Dict, List, Optional

import requests

from core.config import Config
from services.skills.base import SkillBase

logger = logging.getLogger(__name__)


class LogSkill(SkillBase):
    """日志平台连接器"""

    name = "log"
    description = "日志平台连接器，支持 Elasticsearch 和 Loki 后端，提供日志搜索、错误上下文、Trace ID 搜索等能力"
    enabled = Config.SKILL_LOGS_ENABLED
    is_builtin = True

    def __init__(self):
        self.backend = Config.SKILL_LOGS_BACKEND  # 'elasticsearch' 或 'loki'
        self.base_url = Config.SKILL_LOGS_URL.rstrip('/')
        self.index = Config.SKILL_LOGS_INDEX
        self.auth_user = Config.SKILL_LOGS_AUTH_USER
        self.auth_pass = Config.SKILL_LOGS_AUTH_PASS
        self.timeout = 15
        self.config = {
            "backend": self.backend,
            "url": self.base_url,
            "index": self.index,
            "auth_user": self.auth_user,
            "auth_pass": self.auth_pass,
            "timeout": self.timeout
        }

    def update_config(self, config: Dict[str, Any]) -> bool:
        """更新 Log Skill 配置"""
        try:
            if "backend" in config:
                self.backend = config["backend"]
                self.config["backend"] = config["backend"]
            if "url" in config:
                self.base_url = config["url"].rstrip('/')
                self.config["url"] = self.base_url
            if "index" in config:
                self.index = config["index"]
                self.config["index"] = config["index"]
            if "auth_user" in config:
                self.auth_user = config["auth_user"]
                self.config["auth_user"] = config["auth_user"]
            if "auth_pass" in config:
                self.auth_pass = config["auth_pass"]
                self.config["auth_pass"] = config["auth_pass"]
            if "timeout" in config:
                self.timeout = int(config["timeout"])
                self.config["timeout"] = self.timeout
            logger.info(f"Log Skill 配置已更新")
            return True
        except Exception as e:
            logger.error(f"更新 Log Skill 配置失败: {e}")
            return False
    
    def _get_auth(self) -> Optional[tuple]:
        """获取认证信息"""
        if self.auth_user and self.auth_pass:
            return (self.auth_user, self.auth_pass)
        return None
    
    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        headers = {"Content-Type": "application/json"}
        if self.backend == "loki":
            headers["Accept"] = "application/json"
        return headers
    
    def _make_request(self, endpoint: str, data: Dict[str, Any] = None, 
                     params: Dict[str, Any] = None, method: str = "GET") -> Dict[str, Any]:
        """发送 HTTP 请求"""
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers()
        auth = self._get_auth()
        
        try:
            logger.debug(f"Log API 请求: {method} {url}")
            
            if method == "GET":
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    auth=auth,
                    timeout=self.timeout
                )
            else:
                response = requests.post(
                    url,
                    headers=headers,
                    json=data,
                    params=params,
                    auth=auth,
                    timeout=self.timeout
                )
            
            response.raise_for_status()
            return {"success": True, "data": response.json(), "error": None}
        
        except requests.exceptions.Timeout:
            logger.error(f"Log API 请求超时 (>{self.timeout}s)")
            return {"success": False, "data": None, "error": f"请求超时 (>{self.timeout}s)"}
        except requests.exceptions.ConnectionError:
            logger.error(f"无法连接到日志平台: {self.base_url}")
            return {"success": False, "data": None, "error": f"无法连接到日志平台: {self.base_url}"}
        except requests.exceptions.HTTPError as e:
            logger.error(f"Log API HTTP 错误: {e}")
            return {"success": False, "data": None, "error": f"HTTP 错误: {e.response.status_code} - {e.response.text[:200]}"}
        except Exception as e:
            logger.error(f"Log API 请求异常: {e}")
            return {"success": False, "data": None, "error": str(e)}
    
    def get_capabilities(self) -> List[dict]:
        """返回该 Skill 支持的所有操作"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "log__search_logs",
                    "description": "搜索日志，支持关键词查询和时间范围过滤",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索关键词或查询语句"
                            },
                            "time_from": {
                                "type": "string",
                                "description": "开始时间（ISO8601 或相对时间如 '1h ago'）"
                            },
                            "time_to": {
                                "type": "string",
                                "description": "结束时间（ISO8601 或相对时间）"
                            },
                            "source": {
                                "type": "string",
                                "description": "日志来源过滤（如应用名称）"
                            },
                            "limit": {
                                "type": "integer",
                                "description": "返回结果数量限制，默认 50",
                                "default": 50
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "log__get_error_context",
                    "description": "获取错误日志的上下文（前后几行）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "错误关键词或查询语句"
                            },
                            "time_from": {
                                "type": "string",
                                "description": "开始时间"
                            },
                            "time_to": {
                                "type": "string",
                                "description": "结束时间"
                            },
                            "context_lines": {
                                "type": "integer",
                                "description": "上下文行数，默认 5",
                                "default": 5
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "log__get_log_stats",
                    "description": "获取日志统计信息（按时间聚合）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "time_from": {
                                "type": "string",
                                "description": "开始时间"
                            },
                            "time_to": {
                                "type": "string",
                                "description": "结束时间"
                            },
                            "interval": {
                                "type": "string",
                                "description": "聚合间隔，如 '1h', '5m', '1d'，默认 '1h'",
                                "default": "1h"
                            }
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "log__search_by_trace_id",
                    "description": "按 Trace ID 搜索相关日志",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "trace_id": {
                                "type": "string",
                                "description": "Trace ID"
                            }
                        },
                        "required": ["trace_id"]
                    }
                }
            }
        ]
    
    def execute(self, action: str, params: dict) -> dict:
        """执行具体操作"""
        if not self.enabled:
            return {"success": False, "data": None, "error": "Log skill is disabled"}
        
        logger.info(f"执行 Log skill: {action}, backend: {self.backend}, params: {params}")
        
        try:
            if action == "search_logs":
                result = self._search_logs(params)
            elif action == "get_error_context":
                result = self._get_error_context(params)
            elif action == "get_log_stats":
                result = self._get_log_stats(params)
            elif action == "search_by_trace_id":
                result = self._search_by_trace_id(params)
            else:
                return {"success": False, "data": None, "error": f"Unknown action: {action}"}
            
            if result.get("success"):
                logger.info(f"Log skill {action} 执行成功")
                return {"success": True, "data": result.get("data"), "error": None}
            else:
                logger.warning(f"Log skill {action} 执行失败: {result.get('error')}")
                return {"success": False, "data": None, "error": result.get("error")}
        
        except Exception as e:
            logger.error(f"Log skill {action} 执行异常: {e}", exc_info=True)
            return {"success": False, "data": None, "error": str(e)}
    
    def _search_logs(self, params: dict) -> Dict[str, Any]:
        """搜索日志"""
        query = params.get("query")
        time_from = params.get("time_from")
        time_to = params.get("time_to")
        source = params.get("source")
        limit = params.get("limit", 50)
        
        if not query:
            return {"success": False, "data": None, "error": "query 参数不能为空"}
        
        if self.backend == "elasticsearch":
            return self._es_search_logs(query, time_from, time_to, source, limit)
        else:
            return self._loki_search_logs(query, time_from, time_to, source, limit)
    
    def _get_error_context(self, params: dict) -> Dict[str, Any]:
        """获取错误上下文"""
        query = params.get("query")
        time_from = params.get("time_from")
        time_to = params.get("time_to")
        context_lines = params.get("context_lines", 5)
        
        if not query:
            return {"success": False, "data": None, "error": "query 参数不能为空"}
        
        if self.backend == "elasticsearch":
            return self._es_get_error_context(query, time_from, time_to, context_lines)
        else:
            return self._loki_get_error_context(query, time_from, time_to, context_lines)
    
    def _get_log_stats(self, params: dict) -> Dict[str, Any]:
        """获取日志统计"""
        time_from = params.get("time_from")
        time_to = params.get("time_to")
        interval = params.get("interval", "1h")
        
        if self.backend == "elasticsearch":
            return self._es_get_log_stats(time_from, time_to, interval)
        else:
            return self._loki_get_log_stats(time_from, time_to, interval)
    
    def _search_by_trace_id(self, params: dict) -> Dict[str, Any]:
        """按 Trace ID 搜索"""
        trace_id = params.get("trace_id")
        
        if not trace_id:
            return {"success": False, "data": None, "error": "trace_id 参数不能为空"}
        
        if self.backend == "elasticsearch":
            return self._es_search_by_trace_id(trace_id)
        else:
            return self._loki_search_by_trace_id(trace_id)
    
    # ========== Elasticsearch 实现 ==========
    
    def _es_search_logs(self, query: str, time_from: str, time_to: str, 
                        source: str, limit: int) -> Dict[str, Any]:
        """ES 搜索日志"""
        es_query = {
            "query": {
                "bool": {
                    "must": [
                        {"match": {"message": query}}
                    ]
                }
            },
            "sort": [{"@timestamp": {"order": "desc"}}],
            "size": limit
        }
        
        if time_from or time_to:
            range_filter = {"@timestamp": {}}
            if time_from:
                range_filter["@timestamp"]["gte"] = time_from
            if time_to:
                range_filter["@timestamp"]["lte"] = time_to
            es_query["query"]["bool"]["filter"] = [{"range": range_filter}]
        
        if source:
            es_query["query"]["bool"]["must"].append({"match": {"source": source}})
        
        endpoint = f"/{self.index}/_search"
        return self._make_request(endpoint, data=es_query, method="POST")
    
    def _es_get_error_context(self, query: str, time_from: str, time_to: str, 
                              context_lines: int) -> Dict[str, Any]:
        """ES 获取错误上下文"""
        # 先搜索错误日志
        es_query = {
            "query": {
                "bool": {
                    "must": [
                        {"match": {"message": query}}
                    ]
                }
            },
            "sort": [{"@timestamp": {"order": "desc"}}],
            "size": 10
        }
        
        if time_from or time_to:
            range_filter = {"@timestamp": {}}
            if time_from:
                range_filter["@timestamp"]["gte"] = time_from
            if time_to:
                range_filter["@timestamp"]["lte"] = time_to
            es_query["query"]["bool"]["filter"] = [{"range": range_filter}]
        
        endpoint = f"/{self.index}/_search"
        result = self._make_request(endpoint, data=es_query, method="POST")
        
        if not result.get("success"):
            return result
        
        # 获取上下文（简化实现：返回更多日志）
        hits = result.get("data", {}).get("hits", {}).get("hits", [])
        if not hits:
            return {"success": True, "data": {"message": "未找到匹配的日志", "context": []}, "error": None}
        
        # 返回错误日志及其附近日志
        return {"success": True, "data": {"error_logs": hits, "context_lines": context_lines}, "error": None}
    
    def _es_get_log_stats(self, time_from: str, time_to: str, interval: str) -> Dict[str, Any]:
        """ES 获取日志统计"""
        es_query = {
            "query": {"match_all": {}},
            "aggs": {
                "logs_over_time": {
                    "date_histogram": {
                        "field": "@timestamp",
                        "calendar_interval": interval
                    }
                }
            },
            "size": 0
        }
        
        if time_from or time_to:
            range_filter = {"@timestamp": {}}
            if time_from:
                range_filter["@timestamp"]["gte"] = time_from
            if time_to:
                range_filter["@timestamp"]["lte"] = time_to
            es_query["query"] = {"range": range_filter}
        
        endpoint = f"/{self.index}/_search"
        return self._make_request(endpoint, data=es_query, method="POST")
    
    def _es_search_by_trace_id(self, trace_id: str) -> Dict[str, Any]:
        """ES 按 Trace ID 搜索"""
        es_query = {
            "query": {
                "bool": {
                    "should": [
                        {"match": {"trace_id": trace_id}},
                        {"match": {"trace.id": trace_id}},
                        {"match": {"traceId": trace_id}}
                    ]
                }
            },
            "sort": [{"@timestamp": {"order": "asc"}}],
            "size": 100
        }
        
        endpoint = f"/{self.index}/_search"
        return self._make_request(endpoint, data=es_query, method="POST")
    
    # ========== Loki 实现 ==========
    
    def _loki_search_logs(self, query: str, time_from: str, time_to: str, 
                          source: str, limit: int) -> Dict[str, Any]:
        """Loki 搜索日志"""
        # 构建 LogQL 查询
        logql = query
        if source:
            logql = f'{{app="{source}"}} |= `{query}`'
        
        params = {
            "query": logql,
            "limit": limit
        }
        
        if time_from:
            params["start"] = time_from
        if time_to:
            params["end"] = time_to
        
        return self._make_request("/loki/api/v1/query_range", params=params)
    
    def _loki_get_error_context(self, query: str, time_from: str, time_to: str, 
                                context_lines: int) -> Dict[str, Any]:
        """Loki 获取错误上下文"""
        logql = f'|= `{query}`'
        
        params = {
            "query": logql,
            "limit": 10
        }
        
        if time_from:
            params["start"] = time_from
        if time_to:
            params["end"] = time_to
        
        result = self._make_request("/loki/api/v1/query_range", params=params)
        
        if not result.get("success"):
            return result
        
        return {"success": True, "data": {"result": result.get("data"), "context_lines": context_lines}, "error": None}
    
    def _loki_get_log_stats(self, time_from: str, time_to: str, interval: str) -> Dict[str, Any]:
        """Loki 获取日志统计"""
        # Loki 使用聚合查询
        logql = 'sum by (level) (count_over_time({}[1h]))'
        
        params = {
            "query": logql
        }
        
        if time_from:
            params["start"] = time_from
        if time_to:
            params["end"] = time_to
        
        return self._make_request("/loki/api/v1/query_range", params=params)
    
    def _loki_search_by_trace_id(self, trace_id: str) -> Dict[str, Any]:
        """Loki 按 Trace ID 搜索"""
        logql = f'|= `"trace_id":"{trace_id}"` |= `trace_id={trace_id}`'
        
        params = {
            "query": logql,
            "limit": 100
        }
        
        return self._make_request("/loki/api/v1/query_range", params=params)
    
    def health_check(self) -> dict:
        """检查日志平台连接是否可用"""
        if not self.enabled:
            return {"healthy": False, "message": "Log skill is disabled", "details": {}}
        
        try:
            if self.backend == "elasticsearch":
                return self._es_health_check()
            else:
                return self._loki_health_check()
        except Exception as e:
            logger.error(f"Log health check failed: {e}")
            return {
                "healthy": False,
                "message": f"Health check failed: {str(e)}",
                "details": {}
            }
    
    def _es_health_check(self) -> dict:
        """ES 健康检查"""
        url = f"{self.base_url}/_cluster/health"
        auth = self._get_auth()
        
        response = requests.get(url, auth=auth, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        
        status = data.get("status", "unknown")
        healthy = status in ["green", "yellow"]
        
        return {
            "healthy": healthy,
            "message": f"Elasticsearch cluster status: {status}",
            "details": {
                "cluster_name": data.get("cluster_name"),
                "status": status,
                "number_of_nodes": data.get("number_of_nodes"),
                "backend": "elasticsearch"
            }
        }
    
    def _loki_health_check(self) -> dict:
        """Loki 健康检查"""
        url = f"{self.base_url}/ready"
        auth = self._get_auth()
        
        response = requests.get(url, auth=auth, timeout=self.timeout)
        response.raise_for_status()
        
        return {
            "healthy": True,
            "message": "Loki is ready",
            "details": {
                "status": response.text,
                "backend": "loki"
            }
        }


# 全局实例（auto_discover 会扫描到）
log_skill = LogSkill()
