"""Skill 基类和注册表 - 定义统一的平台连接器接口"""

import os
import importlib
import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any

from services.skills.external_loader import discover_external_skills, ExternalSkillAdapter

logger = logging.getLogger(__name__)

# 内置 Skill 名称列表（不允许创建同名自定义 Skill）
BUILTIN_SKILL_NAMES = {'kubernetes', 'prometheus', 'grafana', 'log'}


class SkillBase(ABC):
    """所有平台连接器的基类

    每个 Skill 代表一个外部平台的连接器（如 K8s、Prometheus、Grafana 等）。
    新增平台只需继承此类，实现抽象方法，放入 services/skills/ 目录即可自动注册。
    """

    name: str = ""              # Skill 唯一名称，如 "kubernetes"
    description: str = ""       # 给 AI 看的能力描述，用于 Function Calling
    enabled: bool = True        # 是否启用
    is_builtin: bool = True     # 是否为内置 Skill
    config: Dict[str, Any] = {} # 配置字典

    @abstractmethod
    def get_capabilities(self) -> List[dict]:
        """返回该 Skill 支持的所有操作（供 AI Function Calling 使用）
        
        返回格式：OpenAI function/tool schema 列表
        [
            {
                "type": "function",
                "function": {
                    "name": "kubernetes__get_pod_status",  # {skill_name}__{action_name}
                    "description": "查询指定 Pod 的运行状态",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "namespace": {"type": "string", "description": "K8s 命名空间"},
                            "pod_name": {"type": "string", "description": "Pod 名称"}
                        },
                        "required": ["namespace", "pod_name"]
                    }
                }
            }
        ]
        
        注意：function name 格式为 {skill_name}__{action_name}，用双下划线分隔
        """
        pass
    
    @abstractmethod
    def execute(self, action: str, params: dict) -> dict:
        """执行具体操作
        
        Args:
            action: 操作名称（不含 skill 前缀，如 "get_pod_status"）
            params: 参数字典
        
        Returns:
            {"success": bool, "data": Any, "error": str or None}
        """
        pass
    
    def health_check(self) -> dict:
        """检查连接是否可用
        
        Returns:
            {"healthy": bool, "message": str, "details": dict}
        """
        return {"healthy": True, "message": "Health check not implemented", "details": {}}
    
    def get_info(self) -> dict:
        """获取 Skill 信息摘要"""
        return {
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "is_builtin": self.is_builtin,
            "capabilities_count": len(self.get_capabilities()) if self.enabled else 0,
            "config": self.config
        }

    def update_config(self, config: Dict[str, Any]) -> bool:
        """更新 Skill 配置

        Args:
            config: 新的配置字典

        Returns:
            bool: 是否更新成功
        """
        try:
            self.config.update(config)
            # 子类可以重写此方法以实现特定的配置更新逻辑
            return True
        except Exception as e:
            logger.error(f"更新 Skill '{self.name}' 配置失败: {e}")
            return False


class SkillRegistry:
    """Skill 注册表 - 管理所有已注册的平台连接器"""
    
    def __init__(self):
        self._skills: Dict[str, SkillBase] = {}
    
    def register(self, skill: SkillBase):
        """注册一个 Skill"""
        if not skill.name:
            raise ValueError(f"Skill must have a name: {skill.__class__.__name__}")
        if skill.name in self._skills:
            logger.warning(f"Skill '{skill.name}' already registered, overwriting")
        self._skills[skill.name] = skill
        logger.info(f"Skill registered: {skill.name} ({skill.description})")
    
    def unregister(self, name: str):
        """注销一个 Skill"""
        if name in self._skills:
            del self._skills[name]
            logger.info(f"Skill unregistered: {name}")
    
    def get_skill(self, name: str) -> Optional[SkillBase]:
        """按名称获取 Skill"""
        return self._skills.get(name)
    
    def list_skills(self) -> List[SkillBase]:
        """列出所有已注册的 Skill"""
        return list(self._skills.values())
    
    def list_enabled_skills(self) -> List[SkillBase]:
        """列出所有已启用的 Skill"""
        return [s for s in self._skills.values() if s.enabled]
    
    def get_all_capabilities(self) -> List[dict]:
        """聚合所有已启用 Skill 的能力列表（传给 LLM 做 Function Calling）"""
        capabilities = []
        for skill in self.list_enabled_skills():
            try:
                capabilities.extend(skill.get_capabilities())
            except Exception as e:
                logger.error(f"Failed to get capabilities from skill '{skill.name}': {e}")
        return capabilities
    
    def route_tool_call(self, function_name: str, arguments: dict) -> dict:
        """路由 LLM 的 tool call 到对应 Skill
        
        function_name 格式：{skill_name}__{action_name}
        """
        if '__' not in function_name:
            return {"success": False, "error": f"Invalid function name format: {function_name}"}
        
        skill_name, action_name = function_name.split('__', 1)
        skill = self.get_skill(skill_name)
        
        if not skill:
            return {"success": False, "error": f"Skill not found: {skill_name}"}
        if not skill.enabled:
            return {"success": False, "error": f"Skill is disabled: {skill_name}"}
        
        try:
            return skill.execute(action_name, arguments)
        except Exception as e:
            logger.error(f"Skill execution error: {skill_name}.{action_name} - {e}")
            return {"success": False, "error": str(e)}
    
    def auto_discover(self):
        """自动发现 services/skills/ 目录下的所有 Skill 插件
        
        扫描目录下所有 *_skill.py 文件，导入模块，查找 SkillBase 子类实例
        """
        skills_dir = os.path.dirname(os.path.abspath(__file__))
        logger.info(f"Auto-discovering skills in: {skills_dir}")
        
        for filename in os.listdir(skills_dir):
            if filename.endswith('_skill.py'):
                module_name = filename[:-3]  # 去掉 .py
                try:
                    module = importlib.import_module(f'services.skills.{module_name}')
                    # 查找模块中的 SkillBase 子类实例（约定：每个模块导出一个全局实例）
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if isinstance(attr, SkillBase) and attr.name:
                            self.register(attr)
                except Exception as e:
                    logger.error(f"Failed to load skill module '{module_name}': {e}")
        
        logger.info(f"Auto-discovery complete: {len(self._skills)} skills registered")
        
        # 发现外部 Skill
        self._discover_external_skills()
    
    def _discover_external_skills(self):
        """发现 skills/ 目录下的外部 Skill"""
        from core.config import Config
        
        if not getattr(Config, 'ENABLE_EXTERNAL_SKILLS', True):
            logger.info("外部 Skill 加载已禁用")
            return
        
        skills_dir = getattr(Config, 'EXTERNAL_SKILLS_DIR', 'skills')
        secrets_dir = getattr(Config, 'SKILLS_SECRETS_DIR', 'skills_secrets')
        
        # 将相对路径转为绝对路径（相对于项目根目录）
        if not os.path.isabs(skills_dir):
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            skills_dir = os.path.join(project_root, skills_dir)
        
        if not os.path.isabs(secrets_dir):
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            secrets_dir = os.path.join(project_root, secrets_dir)
        
        external_skills = discover_external_skills(skills_dir, secrets_dir=secrets_dir)
        for skill in external_skills:
            # 避免与已注册的内置 Skill 冲突
            if skill.name in self._skills:
                logger.warning(f"外部 Skill '{skill.name}' 与已注册 Skill 冲突，跳过")
                continue
            self._skills[skill.name] = skill
            logger.info(f"已注册外部 Skill: {skill.name} v{skill.version}")
    
    def reload_external_skills(self):
        """重新扫描并加载外部 Skill"""
        # 先移除已有的外部 Skill
        external_names = [
            name for name, skill in self._skills.items()
            if getattr(skill, 'source', '') == 'external'
        ]
        for name in external_names:
            del self._skills[name]
            logger.info(f"已移除外部 Skill: {name}")
        
        # 重新发现
        self._discover_external_skills()
        
        return self.get_external_skills()
    
    def get_external_skills(self) -> list:
        """获取所有外部 Skill 列表"""
        return [
            skill for skill in self._skills.values()
            if getattr(skill, 'source', '') == 'external'
        ]
    
    def get_status(self) -> dict:
        """获取所有 Skill 的状态摘要"""
        skills_info = []
        for skill in self._skills.values():
            info = skill.get_info()
            try:
                health = skill.health_check()
                info['health'] = health
            except Exception as e:
                info['health'] = {"healthy": False, "message": str(e)}
            skills_info.append(info)
        return {
            "total": len(self._skills),
            "enabled": len(self.list_enabled_skills()),
            "skills": skills_info
        }


    def load_from_db(self):
        """从数据库加载 Skill 配置并更新内置 Skill"""
        try:
            from core.models import SkillConfig, get_session

            session = get_session()
            if session is None:
                logger.warning("Database session not available, skipping skill config loading")
                return

            try:
                configs = session.query(SkillConfig).all()
                for cfg in configs:
                    # 自定义 Skill：加载动态代码
                    if cfg.skill_type == 'custom' and cfg.code:
                        try:
                            self.load_dynamic_skill(cfg.to_dict())
                        except Exception as e:
                            logger.error(f"Failed to load custom skill {cfg.name}: {e}")
                    else:
                        # 更新内置 Skill 的配置
                        skill = self.get_skill(cfg.name)
                        if skill and hasattr(skill, 'update_config'):
                            skill.update_config(cfg.config or {})
                            skill.enabled = cfg.enabled
                            logger.info(f"Updated skill config from DB: {cfg.name}")

                logger.info(f"Loaded {len(configs)} skill configs from database")
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Failed to load skills from DB: {e}")

    def update_skill_config(self, name: str, config: dict, enabled: bool = True):
        """更新指定 Skill 的配置（内存中）"""
        skill = self.get_skill(name)
        if skill:
            if hasattr(skill, 'update_config'):
                skill.update_config(config)
            skill.enabled = enabled
            logger.info(f"Updated skill {name}: enabled={enabled}")

    def is_builtin_name(self, name: str) -> bool:
        """检查名称是否为内置 Skill 名称"""
        return name in BUILTIN_SKILL_NAMES

    def load_dynamic_skill(self, config: dict):
        """加载动态 Skill
        
        Args:
            config: Skill 配置字典，包含 name, code, config, enabled 等
            
        Raises:
            ValueError: 代码语法错误
            RuntimeError: 代码编译失败
        """
        from .dynamic_skill import DynamicSkill
        
        try:
            skill = DynamicSkill(config)
            # 如果已存在同名 skill，先注销
            if config['name'] in self._skills:
                self.unregister(config['name'])
            self.register(skill)
            logger.info(f"Dynamic skill loaded: {config['name']}")
        except Exception as e:
            logger.error(f"Failed to load dynamic skill {config.get('name', 'unknown')}: {e}")
            raise

    def unload_dynamic_skill(self, name: str):
        """卸载动态 Skill
        
        Args:
            name: Skill 名称
        """
        if name in self._skills and not self._skills[name].is_builtin:
            self.unregister(name)
            logger.info(f"Dynamic skill unloaded: {name}")
        elif name in self._skills:
            logger.warning(f"Cannot unload builtin skill: {name}")


# 全局单例
skill_registry = SkillRegistry()
