#!/usr/bin/env python3
"""测试重复告警检测"""

import hashlib
import json

# 你提供的告警数据
alert_data = {
    "AccountId": "2101986858",
    "HappenedAt": "2026-02-26 15:26:13(UTC+08:00)",
    "Level": "warning",
    "Namespace": "VCM_MongoDB_Replica",
    "Project": "default",
    "Resources": [
        {
            "AlertGroupId": "699ff51d7d046683c60b0eff",
            "Dimensions": [
                {
                    "Description": "实例ID",
                    "Name": "ResourceID",
                    "NameCN": "实例ID",
                    "Value": "mongo-replica-c3518fe9c50f",
                },
                {"Description": "节点", "Name": "Node", "NameCN": "节点", "Value": "mongo-replica-c3518fe9c50f-0"},
                {"Description": "实例类型", "Name": "InstanceType", "NameCN": "实例类型", "Value": "ReplicaSet"},
            ],
            "FirstAlertTime": 1772090773,
            "Id": "mongo-replica-c3518fe9c50f",
            "LastAlertTime": 1772090773,
            "Metrics": [
                {
                    "CurrentValue": 99.8549,
                    "Description": "CPU使用率",
                    "DescriptionCN": "CPU使用率",
                    "Name": "CpuUtil",
                    "Threshold": 80,
                    "TriggerCondition": "CPU使用率 统计在最近1个周期内最大值 > 80%，且3个周期内3次满足条件（1周期=1分钟）",
                    "Unit": "%",
                    "Warning": True,
                },
                {
                    "CurrentValue": 7.9454,
                    "Description": "磁盘总使用率",
                    "DescriptionCN": "磁盘总使用率",
                    "Name": "TotalDiskUtil",
                    "Threshold": 85,
                    "TriggerCondition": "磁盘总使用率 统计在最近1个周期内最大值 > 85%，且3个周期内3次满足条件（1周期=1分钟）",
                    "Unit": "%",
                },
                {
                    "CurrentValue": 48.6463,
                    "Description": "内存使用率",
                    "DescriptionCN": "内存使用率",
                    "Name": "MemUtil",
                    "Threshold": 90,
                    "TriggerCondition": "内存使用率 统计在最近1个周期内最大值 > 90%，且3个周期内3次满足条件（1周期=1分钟）",
                    "Unit": "%",
                },
            ],
            "Name": "cyberclone-cn-prod-mongo",
            "ProjectName": "cyberclone-cn",
            "Region": "cn-shanghai",
        }
    ],
    "RuleCondition": "[警告] CPU使用率 统计在最近1个周期内最大值 > 80%，且3个周期内3次满足条件（1周期=1分钟）\n磁盘总使用率 统计在最近1个周期内最大值 > 85%，且3个周期内3次满足条件（1周期=1分钟）\n内存使用率 统计在最近1个周期内最大值 > 90%，且3个周期内3次满足条件（1周期=1分钟）",
    "RuleId": "1995439701473234944",
    "RuleName": "文档数据库 MongoDB 版-副本集副本集告警策略",
    "SubNamespace": "replica",
    "Type": "Metric",
}

# 模拟系统的字段提取逻辑
GENERIC_FIELDS = [
    "Type",
    "RuleName",
    "event",
    "event_type",
    "MetricName",
    "Level",
    "alert_id",
    "alert_name",
    "resource_id",
    "service",
]


def extract_generic_fields(data):
    """提取关键字段（模拟系统逻辑）"""
    key_fields = {}

    # 提取通用字段
    for field in GENERIC_FIELDS:
        if field in data:
            key_fields[field.lower()] = data[field]

    # 提取 Resources
    resources = data.get("Resources", [])
    if isinstance(resources, list) and resources:
        first_resource = resources[0]
        if isinstance(first_resource, dict):
            # 提取资源 ID
            resource_id = first_resource.get("InstanceId") or first_resource.get("Id") or first_resource.get("id")
            if resource_id:
                key_fields["resource_id"] = resource_id

            # 提取 Dimensions
            dimensions = first_resource.get("Dimensions", [])
            if isinstance(dimensions, list):
                for dim in dimensions:
                    if isinstance(dim, dict):
                        dim_name = dim.get("Name", "")
                        dim_value = dim.get("Value")

                        if (
                            dim_name
                            and dim_value
                            and dim_name in ["Node", "ResourceID", "Instance", "InstanceId", "Host", "Pod", "Container"]
                        ):
                            key_fields[f"dim_{dim_name.lower()}"] = dim_value

    return key_fields


def generate_alert_hash(data, source="huaweicloud"):
    """生成告警 hash"""
    key_fields = {"source": source}
    key_fields.update(extract_generic_fields(data))

    # 生成稳定的 JSON 字符串
    key_string = json.dumps(key_fields, sort_keys=True, ensure_ascii=False)

    # 计算 SHA256 hash
    hash_value = hashlib.sha256(key_string.encode("utf-8")).hexdigest()

    return hash_value, key_fields, key_string


# 测试
print("=" * 80)
print("重复告警检测分析")
print("=" * 80)

hash1, fields1, str1 = generate_alert_hash(alert_data, "huaweicloud")
hash2, fields2, str2 = generate_alert_hash(alert_data, "huaweicloud")

print("\n【告警数据1】")
print(f"提取的关键字段: {json.dumps(fields1, indent=2, ensure_ascii=False)}")
print(f"\nHash 字符串: {str1}")
print(f"\n生成的 Hash: {hash1}")

print("\n" + "=" * 80)

print("\n【告警数据2】（相同数据）")
print(f"提取的关键字段: {json.dumps(fields2, indent=2, ensure_ascii=False)}")
print(f"\nHash 字符串: {str2}")
print(f"\n生成的 Hash: {hash2}")

print("\n" + "=" * 80)
print(f"\n✅ Hash 是否相同: {hash1 == hash2}")
print("理论上应该: 被识别为重复告警")

# 分析可能导致不去重的原因
print("\n" + "=" * 80)
print("🔍 可能导致不去重的原因分析:")
print("=" * 80)

reasons = []

# 1. 检查是否有时间戳相关字段
if "HappenedAt" in alert_data:
    reasons.append("❌ HappenedAt 字段未包含在 hash 中（正确，不应包含时间戳）")

if "FirstAlertTime" in alert_data.get("Resources", [{}])[0]:
    reasons.append("❌ FirstAlertTime/LastAlertTime 未包含在 hash 中（正确）")

# 2. 检查关键字段是否都提取了
if "dim_node" in fields1:
    reasons.append("✅ Node 字段已提取: " + fields1["dim_node"])
else:
    reasons.append("⚠️  Node 字段未提取（可能导致不同节点无法区分）")

if "dim_resourceid" in fields1:
    reasons.append("✅ ResourceID 字段已提取: " + fields1["dim_resourceid"])
else:
    reasons.append("⚠️  ResourceID 字段未提取")

if "rulename" in fields1:
    reasons.append("✅ RuleName 字段已提取: " + fields1["rulename"])
else:
    reasons.append("⚠️  RuleName 字段未提取")

# 3. 可能的问题
print("\n实际系统可能的问题:")
print("1. ⏱️  时间窗口: 如果两条告警到达时间间隔超过 DUPLICATE_ALERT_TIME_WINDOW（默认24小时），会被认为是新告警")
print("2. 🔄 并发问题: 如果两条告警几乎同时到达，可能都查不到对方，导致都被认为是新告警")
print("3. 🗄️  数据库问题: 查询或写入时的事务隔离级别可能导致查不到刚插入的记录")
print("4. 📝 日志问题: 需要查看实际日志确认 hash 值和去重逻辑是否正常执行")

for i, reason in enumerate(reasons, 1):
    print(f"{i}. {reason}")

print("\n" + "=" * 80)
print("💡 建议:")
print("=" * 80)
print("1. 查看日志中的 hash 值: grep 'alert_hash' logs/webhook.log")
print("2. 查看重复检测日志: grep '重复告警' logs/webhook.log")
print("3. 检查数据库中的实际记录: SELECT id, alert_hash, is_duplicate FROM webhook_events ORDER BY id DESC LIMIT 5;")
print("4. 确认配置: DUPLICATE_ALERT_TIME_WINDOW 当前值")
