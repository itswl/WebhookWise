"""动态 Skill 加载器 - 支持从数据库加载和执行自定义 Skill 代码"""

import ast
import logging
from typing import Dict, List, Any, Optional, Tuple

from .base import SkillBase

logger = logging.getLogger(__name__)


class DynamicSkill(SkillBase):
    """动态加载的自定义 Skill
    
    从数据库加载 Python 代码并动态执行。
    代码需要定义 capabilities 列表和 execute 函数。
    """
    
    # 标记为非内置 Skill
    is_builtin: bool = False
    
    def __init__(self, config: dict):
        """
        Args:
            config: Skill 配置字典，包含:
                - name: Skill 名称
                - display_name: 显示名称
                - description: 描述
                - enabled: 是否启用
                - config: 配置字典
                - code: Python 代码字符串
        """
        self.name = config['name']
        self.display_name = config.get('display_name', self.name)
        self.description = config.get('description', '')
        self.enabled = config.get('enabled', True)
        self._config = config.get('config', {})
        self._code = config.get('code', '')
        self._capabilities: List[dict] = []
        self._execute_func: Optional[callable] = None
        self._health_check_func: Optional[callable] = None
        
        # 编译并加载代码
        self._compile_code()
    
    def _compile_code(self):
        """编译 Skill 代码"""
        if not self._code:
            logger.warning(f"Dynamic skill {self.name} has no code")
            return
        
        try:
            # 语法检查
            ast.parse(self._code)
            
            # 创建模块命名空间
            module_namespace: Dict[str, Any] = {
                '__name__': f'skill_{self.name}',
                '__file__': f'<dynamic_skill_{self.name}>',
            }
            
            # 执行代码
            exec(self._code, module_namespace)
            
            # 提取 capabilities
            if 'capabilities' in module_namespace:
                self._capabilities = module_namespace['capabilities']
            else:
                logger.warning(f"Dynamic skill {self.name} missing 'capabilities'")
            
            # 提取 execute 函数
            if 'execute' in module_namespace:
                self._execute_func = module_namespace['execute']
            else:
                logger.warning(f"Dynamic skill {self.name} missing 'execute' function")
            
            # 提取 health_check 函数（可选）
            if 'health_check' in module_namespace:
                self._health_check_func = module_namespace['health_check']
            
            logger.info(f"Dynamic skill {self.name} compiled successfully with {len(self._capabilities)} capabilities")
            
        except SyntaxError as e:
            logger.error(f"Syntax error in skill {self.name}: {e}")
            raise ValueError(f"Syntax error: {e}")
        except Exception as e:
            logger.error(f"Failed to compile skill {self.name}: {e}")
            raise RuntimeError(f"Compilation failed: {e}")
    
    def get_capabilities(self) -> List[dict]:
        """返回能力列表"""
        return self._capabilities
    
    def execute(self, action: str, params: dict) -> dict:
        """执行操作
        
        Args:
            action: 操作名称
            params: 操作参数
            
        Returns:
            {"success": bool, "data": Any, "error": str or None}
        """
        if not self._execute_func:
            return {'success': False, 'error': 'Execute function not defined'}
        
        try:
            result = self._execute_func(action, params, self._config)
            # 确保返回格式正确
            if not isinstance(result, dict):
                return {'success': True, 'data': result}
            if 'success' not in result:
                result['success'] = True
            return result
        except Exception as e:
            logger.error(f"Skill {self.name} execution error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
    
    def health_check(self) -> dict:
        """健康检查"""
        if not self._health_check_func:
            return {'healthy': True, 'message': 'No health check defined'}
        
        try:
            result = self._health_check_func(self._config)
            # 确保返回格式正确
            if not isinstance(result, dict):
                return {'healthy': True, 'message': str(result)}
            return result
        except Exception as e:
            logger.error(f"Skill {self.name} health check error: {e}")
            return {'healthy': False, 'message': str(e)}
    
    def reload_code(self, code: str):
        """重新加载代码
        
        Args:
            code: 新的 Python 代码
        """
        self._code = code
        self._compile_code()


def validate_skill_code(code: str) -> Tuple[bool, str]:
    """验证 Skill 代码
    
    检查代码是否符合动态 Skill 的要求：
    1. 语法正确
    2. 定义了 capabilities 列表
    3. 定义了 execute 函数
    
    Args:
        code: Python 代码字符串
        
    Returns:
        (is_valid, error_message)
    """
    if not code or not code.strip():
        return False, "代码为空"
    
    # 语法检查
    try:
        ast.parse(code)
    except SyntaxError as e:
        line_info = f" (行 {e.lineno})" if e.lineno else ""
        return False, f"语法错误{line_info}: {e.msg}"
    
    # 检查必要的组件
    try:
        module_namespace: Dict[str, Any] = {}
        exec(code, module_namespace)
        
        if 'capabilities' not in module_namespace:
            return False, "缺少 'capabilities' 定义"
        
        if 'execute' not in module_namespace:
            return False, "缺少 'execute' 函数"
        
        # 检查 capabilities 格式
        caps = module_namespace['capabilities']
        if not isinstance(caps, list):
            return False, "'capabilities' 必须是列表"
        
        for i, cap in enumerate(caps):
            if not isinstance(cap, dict):
                return False, f"capability[{i}] 必须是字典"
            if 'type' not in cap:
                return False, f"capability[{i}] 缺少 'type' 字段"
            if 'function' not in cap:
                return False, f"capability[{i}] 缺少 'function' 字段"
            if not isinstance(cap.get('function'), dict):
                return False, f"capability[{i}].function 必须是字典"
            if 'name' not in cap['function']:
                return False, f"capability[{i}].function 缺少 'name' 字段"
        
        # 检查 execute 是否可调用
        if not callable(module_namespace['execute']):
            return False, "'execute' 必须是函数"
        
        return True, "验证通过"
        
    except Exception as e:
        return False, f"验证错误: {e}"


def get_skill_template(skill_name: str) -> str:
    """获取 Skill 代码模板
    
    Args:
        skill_name: Skill 名称，用于生成函数名
        
    Returns:
        代码模板字符串
    """
    # 清理 skill_name，确保可以作为函数名的一部分
    safe_name = ''.join(c if c.isalnum() or c == '_' else '_' for c in skill_name)
    
    return f'''"""
自定义 Skill 模板

这是一个示例 Skill，你可以基于此模板创建自己的平台连接器。

关键组件：
1. capabilities: 定义 Skill 的能力列表（OpenAI Function Calling 格式）
2. execute(action, params, config): 执行操作的函数
3. health_check(config): 可选的健康检查函数
"""

# 定义 Skill 的能力列表（OpenAI Function Calling 格式）
capabilities = [
    {{
        "type": "function",
        "function": {{
            "name": "{safe_name}__query_data",
            "description": "查询数据示例",
            "parameters": {{
                "type": "object",
                "properties": {{
                    "query": {{
                        "type": "string",
                        "description": "查询语句"
                    }},
                    "limit": {{
                        "type": "integer",
                        "description": "返回数量限制",
                        "default": 10
                    }}
                }},
                "required": ["query"]
            }}
        }}
    }}
]


def execute(action: str, params: dict, config: dict) -> dict:
    """执行 Skill 操作
    
    Args:
        action: 操作名称（如 "query_data"）
        params: 操作参数
        config: Skill 配置（从数据库读取）
    
    Returns:
        {{"success": bool, "data": Any, "error": str or None}}
    """
    try:
        if action == "query_data":
            # 获取配置
            api_url = config.get('url', '')
            api_token = config.get('token', '')
            
            # 执行查询逻辑
            query = params.get('query', '')
            limit = params.get('limit', 10)
            
            # TODO: 实现实际的查询逻辑
            # 示例：
            # import requests
            # resp = requests.get(
            #     f"{{api_url}}/api/query",
            #     headers={{"Authorization": f"Bearer {{api_token}}"}},
            #     params={{"q": query, "limit": limit}}
            # )
            # data = resp.json()
            
            return {{
                "success": True,
                "data": {{
                    "query": query,
                    "limit": limit,
                    "results": [],
                    "message": "示例响应，请实现实际的查询逻辑"
                }}
            }}
        
        return {{"success": False, "error": f"Unknown action: {{action}}"}}
    
    except Exception as e:
        return {{"success": False, "error": str(e)}}


def health_check(config: dict) -> dict:
    """健康检查（可选）
    
    Returns:
        {{"healthy": bool, "message": str, "details": dict}}
    """
    try:
        api_url = config.get('url', '')
        
        # TODO: 实现实际的健康检查
        # 示例：
        # import requests
        # resp = requests.get(f"{{api_url}}/health", timeout=5)
        # healthy = resp.status_code == 200
        
        return {{
            "healthy": True,
            "message": "健康检查示例，请实现实际的检查逻辑",
            "details": {{"url": api_url}}
        }}
    except Exception as e:
        return {{"healthy": False, "message": str(e), "details": {{}}}}
'''
