#!/usr/bin/env python3
"""
测试多节点告警场景（修复验证）

场景：Redis 集群的两个节点同时触发内存使用率告警
预期：两个节点的告警应该分别处理，不应该被误判为重复告警
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.webhook import WebhookEvent

# 完整的告警数据（节点1）
alert_node1 = {
    "AccountId": "2101986858",
    "Level": "warning",
    "Resources": [
        {
            "Dimensions": [
                {"Name": "ResourceID", "Value": "redis-shzlgorbuxosmn6xz"},
                {"Name": "Node", "Value": "server-redis-shzlgorbuxosmn6xz-000-0-0"},
            ],
            "Id": "redis-shzlgorbuxosmn6xz",
        }
    ],
    "RuleName": "缓存数据库 Redis 版数据节点告警策略",
    "Type": "Metric",
}

# 节点2的告警（只有 Node 不同）
alert_node2 = {
    "AccountId": "2101986858",
    "Level": "warning",
    "Resources": [
        {
            "Dimensions": [
                {"Name": "ResourceID", "Value": "redis-shzlgorbuxosmn6xz"},
                {"Name": "Node", "Value": "server-redis-shzlgorbuxosmn6xz-001-0-0"},
            ],
            "Id": "redis-shzlgorbuxosmn6xz",
        }
    ],
    "RuleName": "缓存数据库 Redis 版数据节点告警策略",
    "Type": "Metric",
}


def test_multi_node_alerts():
    """测试多节点告警去重"""
    print("=" * 80)
    print("测试场景: Redis 集群多节点同时告警")
    print("=" * 80)

    # 生成哈希
    print("\n1️⃣  生成告警哈希:")
    hash1 = WebhookEvent.generate_hash(alert_node1, "volcengine")
    hash2 = WebhookEvent.generate_hash(alert_node2, "volcengine")

    print(f"节点1 哈希: {hash1}")
    print(f"节点2 哈希: {hash2}")

    # 验证结果
    print("\n2️⃣  验证结果:")
    print("=" * 80)

    assert hash1 != hash2
    print("✅ 测试通过！两个不同节点生成了不同的哈希值")

    print("\n3️⃣  额外测试: 相同节点的重复告警（应该被去重）")
    print("=" * 80)

    hash1_duplicate = WebhookEvent.generate_hash(alert_node1, "volcengine")
    assert hash1 == hash1_duplicate
    print("✅ 正确！相同节点的重复告警生成了相同的哈希")

    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)


if __name__ == "__main__":
    test_multi_node_alerts()
