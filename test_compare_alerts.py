#!/usr/bin/env python3
"""
对比两个实际告警
"""
import json
import hashlib


def generate_alert_hash_debug(data, source):
    """生成告警哈希（带调试信息）"""
    key_fields = {
        'source': source,
    }
    
    if isinstance(data, dict):
        # ====== Prometheus Alertmanager 格式 ======
        # 检测是否为 Prometheus Alertmanager 格式（包含 alerts 数组和 alertingRuleName）
        if 'alerts' in data and isinstance(data.get('alerts'), list) and len(data['alerts']) > 0:
            first_alert = data['alerts'][0]
            
            # 告警规则名称（最重要的标识）
            if 'alertingRuleName' in data:
                key_fields['alerting_rule_name'] = data.get('alertingRuleName')
            
            # 告警标签（alertname 是规则ID）
            if isinstance(first_alert.get('labels'), dict):
                labels = first_alert['labels']
                if 'alertname' in labels:
                    key_fields['alertname'] = labels['alertname']
                if 'internal_label_alert_id' in labels:
                    key_fields['alert_id'] = labels['internal_label_alert_id']
                if 'internal_label_alert_level' in labels:
                    key_fields['alert_level'] = labels['internal_label_alert_level']
                
                # 提取资源相关标签（用于区分同一规则的不同资源）
                if 'host' in labels:
                    key_fields['host'] = labels['host']
                if 'instance' in labels:
                    key_fields['instance'] = labels['instance']
                if 'pod' in labels:
                    key_fields['pod'] = labels['pod']
                if 'namespace' in labels:
                    key_fields['namespace'] = labels['namespace']
                if 'service' in labels:
                    key_fields['service'] = labels['service']
                if 'path' in labels:
                    key_fields['path'] = labels['path']
                if 'method' in labels:
                    key_fields['method'] = labels['method']
            
            # 指纹（Prometheus 生成的唯一标识）
            if 'fingerprint' in first_alert:
                key_fields['fingerprint'] = first_alert['fingerprint']
        
        # ====== 华为云监控告警格式 ======
        else:
            # 告警类型和规则名称
            if 'Type' in data:
                key_fields['type'] = data.get('Type')
            if 'RuleName' in data:
                key_fields['rule_name'] = data.get('RuleName')
            if 'event' in data:
                key_fields['event'] = data.get('event')
            if 'event_type' in data:
                key_fields['event_type'] = data.get('event_type')
            
            # 资源标识
            if 'Resources' in data:
                resources = data.get('Resources', [])
                if isinstance(resources, list) and len(resources) > 0:
                    first_resource = resources[0]
                    if isinstance(first_resource, dict):
                        key_fields['resource_id'] = first_resource.get('InstanceId') or first_resource.get('id')
            
            # 指标名称
            if 'MetricName' in data:
                key_fields['metric_name'] = data.get('MetricName')
            
            # 告警级别
            if 'Level' in data:
                key_fields['level'] = data.get('Level')
            
            # 通用字段
            if 'alert_id' in data:
                key_fields['alert_id'] = data.get('alert_id')
            if 'alert_name' in data:
                key_fields['alert_name'] = data.get('alert_name')
            if 'resource_id' in data:
                key_fields['resource_id'] = data.get('resource_id')
            if 'service' in data:
                key_fields['service'] = data.get('service')
    
    # 生成稳定的JSON字符串
    key_string = json.dumps(key_fields, sort_keys=True, ensure_ascii=False)
    
    # 计算SHA256哈希
    hash_value = hashlib.sha256(key_string.encode('utf-8')).hexdigest()
    
    return hash_value, key_fields, key_string


# 告警1
alert1 = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "0a3f5b64-e130-40ec-a51f-1ed589f24310",
                "internal_label_alert_id": "6964b407ca703285636c694f",
                "internal_label_alert_level": "P0"
            },
            "annotations": {
                "__alerting_resource_current_value__": "100",
            },
            "alertURL": "https://console.volcengine.com/prometheus/region:prometheus+cn-shanghai/alert/alerting?detail_id=6964b407ca703285636c694f",
            "startsAt": "2026-01-12T16:42:47+08:00",
            "fingerprint": "d41d8cd98f00b204"
        }
    ],
    "alertingRuleName": "自定义_final_save_slot_memory_success_rate"
}

# 告警2
alert2 = {
    "status": "resolved",
    "alerts": [
        {
            "status": "resolved",
            "labels": {
                "alertname": "21083575-85b4-47c5-b65d-d48a986c974b",
                "host": "chat-backend-20250707.prod.cn.hony.love",
                "internal_label_alert_id": "6964358733b1ddf9b9cfd813",
                "internal_label_alert_level": "P1",
                "method": "POST",
                "path": "/"
            },
            "annotations": {
                "__alerting_resource_current_value__": "2.2749999999999995",
            },
            "alertURL": "https://console.volcengine.com/prometheus/region:prometheus+cn-shanghai/alert/alerting?detail_id=6964358733b1ddf9b9cfd813",
            "startsAt": "2026-01-12T07:47:03+08:00",
            "fingerprint": "b15822399a03bce2"
        }
    ],
    "alertingRuleName": "自定义 ns-hs-sh-prod-k8s Ingress-Nginx 请求时延过高"
}

print("=" * 100)
print("告警1分析: 自定义_final_save_slot_memory_success_rate")
print("=" * 100)
hash1, fields1, key_str1 = generate_alert_hash_debug(alert1, "volcengine-prometheus")
print(f"\n提取的关键字段:")
print(json.dumps(fields1, indent=2, ensure_ascii=False))
print(f"\n关键字段JSON: {key_str1}")
print(f"\n生成的哈希: {hash1}")

print("\n\n" + "=" * 100)
print("告警2分析: 自定义 ns-hs-sh-prod-k8s Ingress-Nginx 请求时延过高")
print("=" * 100)
hash2, fields2, key_str2 = generate_alert_hash_debug(alert2, "volcengine-prometheus")
print(f"\n提取的关键字段:")
print(json.dumps(fields2, indent=2, ensure_ascii=False))
print(f"\n关键字段JSON: {key_str2}")
print(f"\n生成的哈希: {hash2}")

print("\n\n" + "=" * 100)
print("对比结果")
print("=" * 100)

if hash1 == hash2:
    print("⚠️  哈希值相同 - 被判定为重复告警！")
    print(f"哈希值: {hash1}")
else:
    print("✅ 哈希值不同 - 这是两个不同的告警")
    print(f"告警1哈希: {hash1}")
    print(f"告警2哈希: {hash2}")

print("\n关键字段详细对比:")
all_keys = set(fields1.keys()) | set(fields2.keys())
for key in sorted(all_keys):
    val1 = fields1.get(key, "【不存在】")
    val2 = fields2.get(key, "【不存在】")
    status = "✓ 相同" if val1 == val2 else "✗ 不同"
    print(f"\n  {key}: {status}")
    print(f"    告警1: {val1}")
    print(f"    告警2: {val2}")

print("\n\n" + "=" * 100)
print("问题诊断")
print("=" * 100)
print("""
当前去重逻辑存在问题：
1. 这两个告警的数据结构中没有包含标准的去重字段（Type, RuleName, Resources等）
2. 因此提取的 key_fields 只有 'source' 字段
3. 导致所有来自同一来源的告警都生成相同的哈希值
4. 所以被错误地判定为重复告警

解决方案：
需要针对 Prometheus Alertmanager 的告警格式添加特定的字段提取逻辑，
例如提取 alertname, internal_label_alert_id, alertingRuleName 等字段。
""")
