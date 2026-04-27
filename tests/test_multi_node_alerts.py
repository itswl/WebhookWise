#!/usr/bin/env python3
"""
测试多节点告警场景（修复验证）

场景：Redis 集群的两个节点同时触发内存使用率告警
- 节点1: server-redis-shzlgorbuxosmn6xz-000-0-0
- 节点2: server-redis-shzlgorbuxosmn6xz-001-0-0

预期：两个节点的告警应该分别处理，不应该被误判为重复告警
"""
import json

from core.utils import _extract_generic_fields, generate_alert_hash

# 完整的告警数据（节点1）
alert_node1 = {
    "AccountId": "2101986858",
    "HappenedAt": "2026-02-26 09:12:09(UTC+08:00)",
    "Level": "warning",
    "Namespace": "VCM_Redis",
    "Project": "default",
    "Resources": [
        {
            "AlertGroupId": "699f9b1acdfd921a8330f781",
            "Dimensions": [
                {
                    "Description": "实例ID",
                    "Name": "ResourceID",
                    "NameCN": "实例ID",
                    "Value": "redis-shzlgorbuxosmn6xz"
                },
                {
                    "Description": "节点",
                    "Name": "Node",
                    "NameCN": "节点",
                    "Value": "server-redis-shzlgorbuxosmn6xz-000-0-0"
                }
            ],
            "FirstAlertTime": 1772068329,
            "Id": "redis-shzlgorbuxosmn6xz",
            "LastAlertTime": 1772068329,
            "Metrics": [
                {
                    "CurrentValue": 90.5373,
                    "Description": "内存使用率",
                    "DescriptionCN": "内存使用率",
                    "Name": "MemUtil",
                    "Threshold": 90,
                    "TriggerCondition": "内存使用率 统计在最近1个周期内平均值 > 90%，且10个周期内10次满足条件（1周期=1分钟）",
                    "Unit": "%",
                    "Warning": True
                }
            ],
            "Name": "cyberclone-cn-prod-redis",
            "ProjectName": "cyberclone-cn",
            "Region": "cn-shanghai"
        }
    ],
    "RuleCondition": "[警告] 内存使用率 统计在最近1个周期内平均值 > 90%，且10个周期内10次满足条件（1周期=1分钟）",
    "RuleId": "1995439462307655680",
    "RuleName": "缓存数据库 Redis 版数据节点告警策略",
    "SubNamespace": "server",
    "Type": "Metric"
}

# 节点2的告警（只有 Node 不同）
alert_node2 = {
    "AccountId": "2101986858",
    "HappenedAt": "2026-02-26 09:12:09(UTC+08:00)",
    "Level": "warning",
    "Namespace": "VCM_Redis",
    "Project": "default",
    "Resources": [
        {
            "AlertGroupId": "699f9b1acdfd921a8330f782",
            "Dimensions": [
                {
                    "Description": "实例ID",
                    "Name": "ResourceID",
                    "NameCN": "实例ID",
                    "Value": "redis-shzlgorbuxosmn6xz"
                },
                {
                    "Description": "节点",
                    "Name": "Node",
                    "NameCN": "节点",
                    "Value": "server-redis-shzlgorbuxosmn6xz-001-0-0"  # 不同节点
                }
            ],
            "FirstAlertTime": 1772068329,
            "Id": "redis-shzlgorbuxosmn6xz",
            "LastAlertTime": 1772068329,
            "Metrics": [
                {
                    "CurrentValue": 90.5882,  # 略有不同的值
                    "Description": "内存使用率",
                    "DescriptionCN": "内存使用率",
                    "Name": "MemUtil",
                    "Threshold": 90,
                    "TriggerCondition": "内存使用率 统计在最近1个周期内平均值 > 90%，且10个周期内10次满足条件（1周期=1分钟）",
                    "Unit": "%",
                    "Warning": True
                }
            ],
            "Name": "cyberclone-cn-prod-redis",
            "ProjectName": "cyberclone-cn",
            "Region": "cn-shanghai"
        }
    ],
    "RuleCondition": "[警告] 内存使用率 统计在最近1个周期内平均值 > 90%，且10个周期内10次满足条件（1周期=1分钟）",
    "RuleId": "1995439462307655680",
    "RuleName": "缓存数据库 Redis 版数据节点告警策略",
    "SubNamespace": "server",
    "Type": "Metric"
}


def test_multi_node_alerts():
    """测试多节点告警去重"""
    print("=" * 80)
    print("测试场景: Redis 集群多节点同时告警")
    print("=" * 80)

    # 提取关键字段
    print("\n1️⃣  提取节点1的关键字段:")
    fields1 = _extract_generic_fields(alert_node1)
    print(json.dumps(fields1, ensure_ascii=False, indent=2))

    print("\n2️⃣  提取节点2的关键字段:")
    fields2 = _extract_generic_fields(alert_node2)
    print(json.dumps(fields2, ensure_ascii=False, indent=2))

    # 生成哈希
    print("\n3️⃣  生成告警哈希:")
    hash1 = generate_alert_hash(alert_node1, 'volcengine')
    hash2 = generate_alert_hash(alert_node2, 'volcengine')

    print("节点1 (server-redis-shzlgorbuxosmn6xz-000-0-0):")
    print(f"  哈希: {hash1}")

    print("\n节点2 (server-redis-shzlgorbuxosmn6xz-001-0-0):")
    print(f"  哈希: {hash2}")

    # 验证结果
    print("\n4️⃣  验证结果:")
    print("=" * 80)

    assert hash1 != hash2
    print("✅ 测试通过！两个不同节点生成了不同的哈希值")
    print("   节点1和节点2的告警将被正确区分")
    print("\n   关键差异字段:")
    print(f"   - dim_node: '{fields1.get('dim_node')}' vs '{fields2.get('dim_node')}'")

    print("\n5️⃣  额外测试: 相同节点的重复告警（应该被去重）")
    print("=" * 80)

    hash1_duplicate = generate_alert_hash(alert_node1, 'volcengine')
    assert hash1 == hash1_duplicate
    print("✅ 正确！相同节点的重复告警生成了相同的哈希")
    print("   这些告警将被正确去重")

    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)
