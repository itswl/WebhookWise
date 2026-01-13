#!/usr/bin/env python3
"""
测试同一规则的不同告警实例
"""
import sys
sys.path.insert(0, '/Users/imwl/webhooks')

from utils import generate_alert_hash

# 告警1 - 04:47:04 触发，当前值 3.39
alert1 = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "21083575-85b4-47c5-b65d-d48a986c974b",
                "host": "chat-backend-20250707.prod.cn.hony.love",
                "internal_label_alert_id": "69655cd8446159ccbb14307a",
                "internal_label_alert_level": "P1",
                "method": "POST",
                "path": "/"
            },
            "annotations": {
                "__alerting_resource_current_value__": "3.392857142857144",
            },
            "alertURL": "https://console.volcengine.com/prometheus/region:prometheus+cn-shanghai/alert/alerting?detail_id=69655cd8446159ccbb14307a",
            "startsAt": "2026-01-13T04:47:04+08:00",
            "fingerprint": "b15822399a03bce2"
        }
    ],
    "alertingRuleName": "自定义 ns-hs-sh-prod-k8s Ingress-Nginx 请求时延过高"
}

# 告警2 - 07:42:04 触发，当前值 5.21
alert2 = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "21083575-85b4-47c5-b65d-d48a986c974b",
                "host": "chat-backend-20250707.prod.cn.hony.love",
                "internal_label_alert_id": "696585dc33b1ddf9b92dd93e",
                "internal_label_alert_level": "P1",
                "method": "POST",
                "path": "/"
            },
            "annotations": {
                "__alerting_resource_current_value__": "5.214285714285714",
            },
            "alertURL": "https://console.volcengine.com/prometheus/region:prometheus+cn-shanghai/alert/alerting?detail_id=696585dc33b1ddf9b92dd93e",
            "startsAt": "2026-01-13T07:42:04+08:00",
            "fingerprint": "b15822399a03bce2"
        }
    ],
    "alertingRuleName": "自定义 ns-hs-sh-prod-k8s Ingress-Nginx 请求时延过高"
}

hash1 = generate_alert_hash(alert1, "volcengine-prometheus")
hash2 = generate_alert_hash(alert2, "volcengine-prometheus")

print("=" * 100)
print("告警对比分析")
print("=" * 100)

print("\n告警1:")
print(f"  触发时间: 2026-01-13 04:47:04")
print(f"  告警ID: 69655cd8446159ccbb14307a")
print(f"  当前值: 3.39秒")
print(f"  哈希值: {hash1}")

print("\n告警2:")
print(f"  触发时间: 2026-01-13 07:42:04")
print(f"  告警ID: 696585dc33b1ddf9b92dd93e")
print(f"  当前值: 5.21秒")
print(f"  哈希值: {hash2}")

print("\n" + "=" * 100)
print("关键字段对比:")
print("=" * 100)
print(f"  alertingRuleName: ✓ 相同 - '自定义 ns-hs-sh-prod-k8s Ingress-Nginx 请求时延过高'")
print(f"  alertname: ✓ 相同 - '21083575-85b4-47c5-b65d-d48a986c974b'")
print(f"  host: ✓ 相同 - 'chat-backend-20250707.prod.cn.hony.love'")
print(f"  path: ✓ 相同 - '/'")
print(f"  method: ✓ 相同 - 'POST'")
print(f"  fingerprint: ✓ 相同 - 'b15822399a03bce2'")
print(f"  alert_level: ✓ 相同 - 'P1'")
print(f"\n  internal_label_alert_id: ✗ 不同")
print(f"    告警1: 69655cd8446159ccbb14307a")
print(f"    告警2: 696585dc33b1ddf9b92dd93e")
print(f"  当前值: ✗ 不同 (3.39 vs 5.21)")
print(f"  触发时间: ✗ 不同 (04:47 vs 07:42)")

print("\n" + "=" * 100)
print("判定结果:")
print("=" * 100)

if hash1 == hash2:
    print("✅ 哈希值相同 - 被判定为重复告警（正确）")
    print(f"   这是同一个告警规则在同一资源上的持续触发")
    print(f"   虽然 alert_id 和当前值不同，但应该视为同一告警的不同时间点")
else:
    print("❌ 哈希值不同 - 被判定为不同告警（错误）")
    print(f"   告警1哈希: {hash1}")
    print(f"   告警2哈希: {hash2}")
    print(f"\n   问题: internal_label_alert_id 被包含在哈希计算中")
    print(f"   这导致同一规则、同一资源的持续告警被误判为不同告警")

print("\n" + "=" * 100)
print("建议:")
print("=" * 100)
print("""
internal_label_alert_id 是每次告警触发时生成的唯一ID，
不应该参与哈希计算，否则同一告警的持续触发会被误判为新告警。

应该保留的字段（用于区分不同告警）:
  ✓ alertingRuleName - 告警规则名称
  ✓ alertname - 告警规则ID
  ✓ host/pod/instance - 资源标识
  ✓ path/method - 请求路径和方法
  ✓ fingerprint - Prometheus 生成的指纹

应该移除的字段（不用于去重）:
  ✗ internal_label_alert_id - 每次触发的唯一ID
  ✗ __alerting_resource_current_value__ - 当前值（会变化）
  ✗ startsAt - 触发时间（会变化）
""")
