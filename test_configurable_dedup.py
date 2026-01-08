#!/usr/bin/env python3
"""
测试可配置的重复告警去重功能
"""
import requests
import json
import time

# Webhook 服务地址
BASE_URL = "http://localhost:8000"
WEBHOOK_URL = f"{BASE_URL}/webhook"
CONFIG_URL = f"{BASE_URL}/api/config"

# 模拟云监控告警数据
alert_data = {
    "Type": "AlarmNotification",
    "RuleName": "内存使用率告警",
    "Level": "critical",
    "MetricName": "MemoryUtilization",
    "CurrentValue": 92.5,
    "Threshold": 85.0,
    "Resources": [
        {
            "InstanceId": "i-test-001",
            "Region": "cn-beijing"
        }
    ],
    "AlarmTime": "2025-11-07T12:00:00Z"
}

def get_config():
    """获取当前配置"""
    response = requests.get(CONFIG_URL)
    return response.json()

def update_config(config_data):
    """更新配置"""
    response = requests.post(CONFIG_URL, json=config_data)
    return response.json()

def send_webhook(data, source="cloud-monitor"):
    """发送 webhook 请求"""
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Source": source
    }
    
    response = requests.post(WEBHOOK_URL, json=data, headers=headers)
    return response.json()

def test_time_window_config():
    """测试时间窗口配置"""
    print("=" * 60)
    print("测试1: 时间窗口配置")
    print("=" * 60)
    
    # 获取当前配置
    print("\n1. 获取当前配置...")
    config = get_config()
    if config.get('success'):
        current_window = config['data'].get('duplicate_alert_time_window', 24)
        print(f"   当前时间窗口: {current_window} 小时")
    
    # 修改时间窗口为1小时
    print("\n2. 修改时间窗口为1小时...")
    update_result = update_config({
        'duplicate_alert_time_window': 1
    })
    print(f"   更新结果: {json.dumps(update_result, ensure_ascii=False)}")
    
    # 验证配置已更新
    print("\n3. 验证配置已更新...")
    config = get_config()
    if config.get('success'):
        new_window = config['data'].get('duplicate_alert_time_window')
        print(f"   新的时间窗口: {new_window} 小时")
        if new_window == 1:
            print("   ✓ 时间窗口配置更新成功")
        else:
            print("   ✗ 时间窗口配置更新失败")
    
    # 恢复默认配置
    print("\n4. 恢复默认配置...")
    update_config({'duplicate_alert_time_window': 24})
    print("   ✓ 已恢复为24小时")

def test_forward_duplicate_config():
    """测试重复告警转发配置"""
    print("\n" + "=" * 60)
    print("测试2: 重复告警转发配置")
    print("=" * 60)
    
    # 获取当前配置
    print("\n1. 获取当前配置...")
    config = get_config()
    if config.get('success'):
        forward_dup = config['data'].get('forward_duplicate_alerts', False)
        print(f"   是否转发重复告警: {forward_dup}")
    
    # 关闭重复告警转发
    print("\n2. 关闭重复告警转发...")
    update_result = update_config({
        'forward_duplicate_alerts': False
    })
    print(f"   更新结果: {json.dumps(update_result, ensure_ascii=False)}")
    
    # 发送高风险告警
    print("\n3. 发送高风险告警（第一次）...")
    result1 = send_webhook(alert_data)
    print(f"   是否重复: {result1.get('is_duplicate', False)}")
    print(f"   转发状态: {result1.get('forward_status', 'unknown')}")
    
    time.sleep(1)
    
    # 发送相同的高风险告警
    print("\n4. 发送相同的高风险告警（第二次，应该不转发）...")
    result2 = send_webhook(alert_data)
    print(f"   是否重复: {result2.get('is_duplicate', False)}")
    print(f"   转发状态: {result2.get('forward_status', 'unknown')}")
    
    if result2.get('is_duplicate') and result2.get('forward_status') == 'skipped':
        print("   ✓ 重复告警正确跳过转发")
    else:
        print("   ✗ 重复告警应该跳过转发")
    
    # 开启重复告警转发
    print("\n5. 开启重复告警转发...")
    update_config({'forward_duplicate_alerts': True})
    
    time.sleep(1)
    
    # 再次发送相同告警
    print("\n6. 再次发送相同告警（第三次，应该转发）...")
    result3 = send_webhook(alert_data)
    print(f"   是否重复: {result3.get('is_duplicate', False)}")
    print(f"   转发状态: {result3.get('forward_status', 'unknown')}")
    
    if result3.get('is_duplicate'):
        if result3.get('forward_status') in ['success', 'failed', 'timeout']:
            print("   ✓ 重复告警正确执行转发")
        else:
            print(f"   ! 重复告警转发状态: {result3.get('forward_status')}")
    
    # 恢复默认配置
    print("\n7. 恢复默认配置（关闭重复告警转发）...")
    update_config({'forward_duplicate_alerts': False})
    print("   ✓ 已恢复默认配置")

def test_custom_time_window():
    """测试自定义时间窗口"""
    print("\n" + "=" * 60)
    print("测试3: 自定义短时间窗口")
    print("=" * 60)
    
    # 设置时间窗口为非常短（例如0.001小时，即几秒）
    # 注意：这只是演示，实际使用中不建议设置这么短
    print("\n1. 设置极短时间窗口用于测试...")
    update_config({'duplicate_alert_time_window': 0.001})
    
    # 发送告警
    print("\n2. 发送告警...")
    different_alert = alert_data.copy()
    different_alert["Resources"] = [{"InstanceId": "i-test-002", "Region": "cn-beijing"}]
    result1 = send_webhook(different_alert)
    print(f"   Webhook ID: {result1.get('webhook_id')}")
    
    # 等待超过时间窗口
    print("\n3. 等待5秒（超过时间窗口）...")
    time.sleep(5)
    
    # 再次发送相同告警（由于超过时间窗口，应该被视为新告警）
    print("\n4. 再次发送相同告警...")
    result2 = send_webhook(different_alert)
    print(f"   是否重复: {result2.get('is_duplicate', False)}")
    print(f"   Webhook ID: {result2.get('webhook_id')}")
    
    if not result2.get('is_duplicate'):
        print("   ✓ 超过时间窗口后，正确识别为新告警")
    else:
        print("   ✗ 应该识别为新告警")
    
    # 恢复默认配置
    print("\n5. 恢复默认时间窗口...")
    update_config({'duplicate_alert_time_window': 24})
    print("   ✓ 已恢复为24小时")

def main():
    """主测试函数"""
    print("开始测试可配置的重复告警去重功能\n")
    
    try:
        # 测试1：时间窗口配置
        test_time_window_config()
        
        time.sleep(2)
        
        # 测试2：重复告警转发配置
        test_forward_duplicate_config()
        
        time.sleep(2)
        
        # 测试3：自定义时间窗口
        test_custom_time_window()
        
        print("\n" + "=" * 60)
        print("所有测试完成！")
        print("=" * 60)
        
    except requests.exceptions.ConnectionError:
        print("错误: 无法连接到 webhook 服务，请确保服务已启动")
        print("运行: python app.py")
    except Exception as e:
        print(f"错误: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()
