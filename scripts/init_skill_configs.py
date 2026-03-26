#!/usr/bin/env python3
"""
初始化内置 Skill 平台连接配置到数据库

从 Config 类读取 .env 中的 Skill 配置，初始化到 skill_configs 表
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.models import SkillConfig, session_scope, init_db


# 内置 Skill 配置定义
BUILTIN_SKILLS = [
    {
        'name': 'kubernetes',
        'display_name': 'Kubernetes',
        'description': 'Kubernetes 集群连接，支持查询 Pod、Deployment、Node 等资源状态',
        'skill_type': 'kubernetes',
        'enabled': Config.SKILL_K8S_ENABLED,
        'config': {
            'kubeconfig': Config.SKILL_K8S_KUBECONFIG,
            'context': Config.SKILL_K8S_CONTEXT,
        }
    },
    {
        'name': 'prometheus',
        'display_name': 'Prometheus',
        'description': 'Prometheus 监控系统连接，支持查询指标数据和告警规则',
        'skill_type': 'prometheus',
        'enabled': Config.SKILL_PROMETHEUS_ENABLED,
        'config': {
            'url': Config.SKILL_PROMETHEUS_URL,
            'auth_token': Config.SKILL_PROMETHEUS_AUTH_TOKEN,
        }
    },
    {
        'name': 'grafana',
        'display_name': 'Grafana',
        'description': 'Grafana 可视化平台连接，支持查询仪表盘和面板数据',
        'skill_type': 'grafana',
        'enabled': Config.SKILL_GRAFANA_ENABLED,
        'config': {
            'url': Config.SKILL_GRAFANA_URL,
            'api_token': Config.SKILL_GRAFANA_API_TOKEN,
        }
    },
    {
        'name': 'logs',
        'display_name': '日志平台',
        'description': f'日志平台连接（{Config.SKILL_LOGS_BACKEND}），支持日志搜索和分析',
        'skill_type': 'log',
        'enabled': Config.SKILL_LOGS_ENABLED,
        'config': {
            'backend': Config.SKILL_LOGS_BACKEND,
            'url': Config.SKILL_LOGS_URL,
            'index': Config.SKILL_LOGS_INDEX,
            'auth_user': Config.SKILL_LOGS_AUTH_USER,
            'auth_pass': Config.SKILL_LOGS_AUTH_PASS,
        }
    },
]


def init_skill_configs():
    """初始化内置 Skill 配置到数据库"""
    print("=" * 60)
    print("初始化内置 Skill 平台连接配置")
    print("=" * 60)
    
    # 确保表存在
    init_db()
    
    created_count = 0
    skipped_count = 0
    
    with session_scope() as session:
        for skill_def in BUILTIN_SKILLS:
            name = skill_def['name']
            
            # 检查是否已存在
            existing = session.query(SkillConfig).filter_by(name=name).first()
            
            if existing:
                print(f"  [跳过] {skill_def['display_name']} ({name}) - 已存在")
                skipped_count += 1
                continue
            
            # 创建新记录
            skill = SkillConfig(
                name=skill_def['name'],
                display_name=skill_def['display_name'],
                description=skill_def['description'],
                skill_type=skill_def['skill_type'],
                enabled=skill_def['enabled'],
                config=skill_def['config'],
            )
            session.add(skill)
            
            status = "启用" if skill_def['enabled'] else "禁用"
            print(f"  [创建] {skill_def['display_name']} ({name}) - {status}")
            created_count += 1
    
    print("-" * 60)
    print(f"操作完成: 创建 {created_count} 条记录, 跳过 {skipped_count} 条已存在记录")
    print("=" * 60)


if __name__ == '__main__':
    init_skill_configs()
