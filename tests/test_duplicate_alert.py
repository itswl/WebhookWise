#!/usr/bin/env python3
"""
测试重复告警去重功能
"""
import requests
import json
import time

# Webhook 服务地址
WEBHOOK_URL = "http://localhost:8000/webhook"

# 模拟云监控告警数据
alert_data = {
    "Type": "AlarmNotification",
    "RuleName": "CPU使用率告警",
    "Level": "critical",
    "MetricName": "CPUUtilization",
    "CurrentValue": 95.5,
    "Threshold": 80.0,
    "Resources": [
        {
            "InstanceId": "i-abc123",
            "Region": "cn-hangzhou"
        }
    ],
    "AlarmTime": "2025-11-07T11:00:00Z"
}

def send_webhook(data, source="cloud-monitor"):
    """发送 webhook 请求"""
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Source": source
    }
    
    response = requests.post(WEBHOOK_URL, json=data, headers=headers)
    return response.json()

def test_duplicate_detection():
    """测试重复告警检测"""
    print("=" * 60)
    print("测试重复告警去重功能")
    print("=" * 60)
    
    # 第一次发送告警（新告警）
    print("\n1. 发送第一次告警（新告警）...")
    result1 = send_webhook(alert_data)
    print(f"   响应: {json.dumps(result1, ensure_ascii=False, indent=2)}")
    print(f"   是否重复: {result1.get('is_duplicate', False)}")
    print(f"   Webhook ID: {result1.get('webhook_id')}")
    
    time.sleep(1)
    
    # 第二次发送相同告警（应该被识别为重复）
    print("\n2. 发送第二次相同告警（应该被识别为重复）...")
    result2 = send_webhook(alert_data)
    print(f"   响应: {json.dumps(result2, ensure_ascii=False, indent=2)}")
    print(f"   是否重复: {result2.get('is_duplicate', False)}")
    print(f"   Webhook ID: {result2.get('webhook_id')}")
    print(f"   原始告警ID: {result2.get('duplicate_of')}")
    
    time.sleep(1)
    
    # 第三次发送相同告警
    print("\n3. 发送第三次相同告警...")
    result3 = send_webhook(alert_data)
    print(f"   响应: {json.dumps(result3, ensure_ascii=False, indent=2)}")
    print(f"   是否重复: {result3.get('is_duplicate', False)}")
    print(f"   Webhook ID: {result3.get('webhook_id')}")
    print(f"   原始告警ID: {result3.get('duplicate_of')}")
    
    time.sleep(1)
    
    # 修改告警数据，发送不同的告警
    print("\n4. 发送不同的告警（修改资源ID）...")
    different_alert = alert_data.copy()
    different_alert["Resources"] = [
        {
            "InstanceId": "i-xyz789",  # 不同的实例ID
            "Region": "cn-hangzhou"
        }
    ]
    result4 = send_webhook(different_alert)
    print(f"   响应: {json.dumps(result4, ensure_ascii=False, indent=2)}")
    print(f"   是否重复: {result4.get('is_duplicate', False)}")
    print(f"   Webhook ID: {result4.get('webhook_id')}")
    
    print("\n" + "=" * 60)
    print("测试完成！")
    print("=" * 60)
    
    # 验证结果
    print("\n验证结果:")
    if result1.get('is_duplicate') == False:
        print("✓ 第一次告警正确识别为新告警")
    else:
        print("✗ 第一次告警应该是新告警")
    
    if result2.get('is_duplicate') == True:
        print("✓ 第二次告警正确识别为重复告警")
    else:
        print("✗ 第二次告警应该是重复告警")
    
    if result3.get('is_duplicate') == True:
        print("✓ 第三次告警正确识别为重复告警")
    else:
        print("✗ 第三次告警应该是重复告警")
    
    if result4.get('is_duplicate') == False:
        print("✓ 不同的告警正确识别为新告警")
    else:
        print("✗ 不同的告警应该是新告警")


if __name__ == '__main__':
    try:
        test_duplicate_detection()
    except requests.exceptions.ConnectionError:
        print("错误: 无法连接到 webhook 服务，请确保服务已启动")
        print("运行: python app.py")
    except Exception as e:
        print(f"错误: {str(e)}")
