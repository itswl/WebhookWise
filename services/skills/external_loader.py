"""外部 Skill 加载器 - 从 skills/ 目录发现和加载外部 Skill"""

import os
import json
import re
import logging
import subprocess
from typing import Dict, List, Any, Optional

logger = logging.getLogger('webhook_service')


class ExternalSkillAdapter:
    """外部 Skill 的通用适配器
    
    支持从 skills/ 目录加载 SKILL.md + _meta.json 格式的 Skill。
    不继承 SkillBase（因为外部 Skill 的执行方式不同），
    但提供兼容的接口供注册表使用。
    """
    
    def __init__(self, skill_dir: str, secrets_dir: str = None):
        self.skill_dir = skill_dir
        self.secrets_dir = secrets_dir
        self.name = ''
        self.display_name = ''
        self.description = ''
        self.version = '1.0.0'
        self.slug = ''
        self.owner_id = ''
        self.enabled = True
        self.source = 'external'
        self.skill_content = ''  # SKILL.md 完整内容
        self.has_scripts = False
        self.scripts = []
        self.secrets: Dict[str, str] = {}  # 私密配置
        
        self._load_metadata()
        self._detect_scripts()
        self._load_secrets()
    
    def _load_metadata(self):
        """从 _meta.json 和 SKILL.md 加载元数据"""
        # 1. 解析 _meta.json
        meta_path = os.path.join(self.skill_dir, '_meta.json')
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                self.slug = meta.get('slug', '')
                self.version = meta.get('version', '1.0.0')
                self.owner_id = meta.get('ownerId', '')
                self.name = self.slug or os.path.basename(self.skill_dir).split('-')[0]
            except Exception as e:
                logger.warning(f"解析 _meta.json 失败: {meta_path}, {e}")
                self.name = os.path.basename(self.skill_dir)
        
        # 2. 解析 SKILL.md frontmatter 和内容
        skill_md_path = os.path.join(self.skill_dir, 'SKILL.md')
        if os.path.exists(skill_md_path):
            try:
                with open(skill_md_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                self.skill_content = content
                
                # 解析 YAML frontmatter (--- ... ---)
                frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
                if frontmatter_match:
                    fm_text = frontmatter_match.group(1)
                    # 简单解析 key: value 格式
                    for line in fm_text.split('\n'):
                        line = line.strip()
                        if ':' in line:
                            key, _, value = line.partition(':')
                            key = key.strip()
                            value = value.strip().strip('"').strip("'")
                            if key == 'name' and not self.name:
                                self.name = value
                            elif key == 'description':
                                self.description = value
                
                # 如果没有 frontmatter，从第一个 # 标题提取名称
                if not self.display_name:
                    title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
                    if title_match:
                        self.display_name = title_match.group(1).strip()
                
                if not self.display_name:
                    self.display_name = self.name
                    
            except Exception as e:
                logger.warning(f"解析 SKILL.md 失败: {skill_md_path}, {e}")
        
        # 兜底：用目录名
        if not self.name:
            dir_name = os.path.basename(self.skill_dir)
            # 去掉版本号后缀，如 k8s-browser-1.0.0 -> k8s-browser
            parts = dir_name.rsplit('-', 1)
            if len(parts) == 2 and re.match(r'\d+\.\d+', parts[1]):
                self.name = parts[0]
            else:
                self.name = dir_name
        
        if not self.display_name:
            self.display_name = self.name
    
    def _detect_scripts(self):
        """检测 Skill 是否包含可执行脚本"""
        scripts_dir = os.path.join(self.skill_dir, 'scripts')
        if os.path.isdir(scripts_dir):
            self.has_scripts = True
            self.scripts = [
                f for f in os.listdir(scripts_dir)
                if os.path.isfile(os.path.join(scripts_dir, f))
            ]
    
    def _load_secrets(self):
        """从 skills_secrets/{skill_name}.json 或 .env 加载私密配置"""
        if not self.secrets_dir:
            return
        
        # 使用 slug 或 name 作为文件名
        secret_name = self.slug or self.name
        if not secret_name:
            return
        
        # 优先尝试 JSON 格式
        json_path = os.path.join(self.secrets_dir, f'{secret_name}.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    secrets = json.load(f)
                if isinstance(secrets, dict):
                    self.secrets = {k: str(v) for k, v in secrets.items()}
                    logger.info(f"从 {json_path} 加载了 {len(self.secrets)} 个 secrets")
            except Exception as e:
                logger.warning(f"解析 secrets JSON 失败: {json_path}, {e}")
            return
        
        # 尝试 .env 格式
        env_path = os.path.join(self.secrets_dir, f'{secret_name}.env')
        if os.path.exists(env_path):
            try:
                with open(env_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        # 跳过空行和注释
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            key, _, value = line.partition('=')
                            key = key.strip()
                            value = value.strip().strip('"').strip("'")
                            if key:
                                self.secrets[key] = value
                logger.info(f"从 {env_path} 加载了 {len(self.secrets)} 个 secrets")
            except Exception as e:
                logger.warning(f"解析 secrets env 失败: {env_path}, {e}")
    
    def get_capabilities(self) -> List[dict]:
        """返回 Skill 的能力描述（OpenAI function schema 格式）"""
        capabilities = []
        
        # 通用能力：获取 Skill 文档
        capabilities.append({
            'type': 'function',
            'function': {
                'name': f'{self.name}__get_guide',
                'description': f'获取 {self.display_name} 的使用指南和文档',
                'parameters': {
                    'type': 'object',
                    'properties': {},
                    'required': []
                }
            }
        })
        
        # 如果有脚本，为每个脚本添加能力
        if self.has_scripts:
            for script in self.scripts:
                script_name = os.path.splitext(script)[0]
                capabilities.append({
                    'type': 'function',
                    'function': {
                        'name': f'{self.name}__{script_name}',
                        'description': f'执行 {self.display_name} 的 {script_name} 脚本',
                        'parameters': {
                            'type': 'object',
                            'properties': {
                                'args': {
                                    'type': 'string',
                                    'description': '传递给脚本的参数'
                                }
                            },
                            'required': []
                        }
                    }
                })
        
        return capabilities
    
    def execute(self, action: str, params: dict = None) -> dict:
        """执行 Skill 操作"""
        params = params or {}
        
        if action == 'get_guide':
            return {
                'success': True,
                'data': {
                    'name': self.display_name,
                    'version': self.version,
                    'content': self.skill_content
                }
            }
        
        # 脚本执行
        if self.has_scripts:
            script_file = None
            for s in self.scripts:
                if os.path.splitext(s)[0] == action:
                    script_file = s
                    break
            
            if script_file:
                script_path = os.path.join(self.skill_dir, 'scripts', script_file)
                try:
                    # 构建环境变量：继承当前环境 + 注入 secrets
                    env = os.environ.copy()
                    env.update(self.secrets)
                    
                    result = subprocess.run(
                        [script_path] + (params.get('args', '').split() if params.get('args') else []),
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=self.skill_dir,
                        env=env
                    )
                    return {
                        'success': result.returncode == 0,
                        'data': {
                            'stdout': result.stdout,
                            'stderr': result.stderr,
                            'returncode': result.returncode
                        }
                    }
                except subprocess.TimeoutExpired:
                    return {'success': False, 'error': '脚本执行超时（30秒）'}
                except Exception as e:
                    return {'success': False, 'error': str(e)}
        
        return {'success': False, 'error': f'未知操作: {action}'}
    
    def health_check(self) -> dict:
        """检查 Skill 目录和文件完整性"""
        issues = []
        
        if not os.path.isdir(self.skill_dir):
            issues.append('Skill 目录不存在')
        
        skill_md = os.path.join(self.skill_dir, 'SKILL.md')
        if not os.path.exists(skill_md):
            issues.append('缺少 SKILL.md')
        
        meta_json = os.path.join(self.skill_dir, '_meta.json')
        if not os.path.exists(meta_json):
            issues.append('缺少 _meta.json')
        
        return {
            'healthy': len(issues) == 0,
            'issues': issues,
            'has_scripts': self.has_scripts,
            'scripts_count': len(self.scripts)
        }
    
    def to_dict(self) -> dict:
        """序列化为字典（不输出 secrets 值，只输出 has_secrets）"""
        health = self.health_check()
        return {
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'version': self.version,
            'slug': self.slug,
            'source': self.source,
            'enabled': self.enabled,
            'has_scripts': self.has_scripts,
            'scripts': self.scripts,
            'scripts_count': len(self.scripts),
            'has_secrets': len(self.secrets) > 0,
            'skill_dir': self.skill_dir,
            'healthy': health.get('healthy', False),
            'health': health
        }


def discover_external_skills(base_dir: str, secrets_dir: str = None) -> List[ExternalSkillAdapter]:
    """发现指定目录下的所有外部 Skill
    
    Args:
        base_dir: 外部 Skill 根目录路径
        secrets_dir: Skill secrets 目录路径
    
    Returns:
        ExternalSkillAdapter 实例列表
    """
    skills = []
    
    if not os.path.isdir(base_dir):
        logger.info(f"外部 Skill 目录不存在: {base_dir}")
        return skills
    
    for entry in sorted(os.listdir(base_dir)):
        skill_path = os.path.join(base_dir, entry)
        if not os.path.isdir(skill_path):
            continue
        
        # 检查必要文件
        skill_md = os.path.join(skill_path, 'SKILL.md')
        if not os.path.exists(skill_md):
            continue
        
        try:
            adapter = ExternalSkillAdapter(skill_path, secrets_dir=secrets_dir)
            skills.append(adapter)
            logger.info(f"发现外部 Skill: {adapter.name} v{adapter.version} ({skill_path})")
        except Exception as e:
            logger.warning(f"加载外部 Skill 失败: {skill_path}, {e}")
    
    logger.info(f"共发现 {len(skills)} 个外部 Skill")
    return skills
