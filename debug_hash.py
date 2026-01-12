#!/usr/bin/env python3
"""
调试告警哈希生成工具
用于查看两个告警的哈希生成过程，帮助理解为什么被判定为重复
"""
import json
import hashlib


def generate_alert_hash_debug(data, source):
    """
    生成告警哈希（带调试信息）
    """
    key_fields = {
        'source': source,
    }
    
    # 云监控告警特定字段
    if isinstance(data, dict):
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


def analyze_alert(alert_json_str, source="unknown"):
    """
    分析告警数据
    """
    try:
        data = json.loads(alert_json_str)
        hash_value, key_fields, key_string = generate_alert_hash_debug(data, source)
        
        print("=" * 80)
        print("告警数据分析")
        print("=" * 80)
        print(f"\n来源: {source}")
        print(f"\n生成哈希的关键字段:")
        print(json.dumps(key_fields, indent=2, ensure_ascii=False))
        print(f"\n关键字段JSON字符串:")
        print(key_string)
        print(f"\n生成的哈希值:")
        print(hash_value)
        print("=" * 80)
        
        return hash_value, key_fields
        
    except json.JSONDecodeError as e:
        print(f"❌ JSON解析错误: {e}")
        return None, None


def compare_alerts(alert1_json, alert2_json, source="unknown"):
    """
    比较两个告警
    """
    print("\n🔍 告警1分析:")
    hash1, fields1 = analyze_alert(alert1_json, source)
    
    print("\n🔍 告警2分析:")
    hash2, fields2 = analyze_alert(alert2_json, source)
    
    if hash1 and hash2:
        print("\n" + "=" * 80)
        print("对比结果")
        print("=" * 80)
        
        if hash1 == hash2:
            print("⚠️  两个告警的哈希值相同 - 会被判定为重复告警")
            print(f"哈希值: {hash1}")
            
            print("\n关键字段对比:")
            all_keys = set(fields1.keys()) | set(fields2.keys())
            for key in sorted(all_keys):
                val1 = fields1.get(key, "【不存在】")
                val2 = fields2.get(key, "【不存在】")
                status = "✓ 相同" if val1 == val2 else "✗ 不同"
                print(f"  {key}: {status}")
                print(f"    告警1: {val1}")
                print(f"    告警2: {val2}")
        else:
            print("✓ 两个告警的哈希值不同 - 不会被判定为重复")
            print(f"告警1哈希: {hash1}")
            print(f"告警2哈希: {hash2}")


if __name__ == "__main__":
    print("告警哈希调试工具")
    print("请粘贴你的告警JSON数据进行分析")
    print("\n使用方法:")
    print("1. 单个告警分析: python debug_hash.py")
    print("   然后粘贴JSON数据")
    print("\n2. 比较两个告警: 修改下面的示例代码")
    
    # 示例：比较两个告警
    # 取消注释并替换为你的实际数据
    """
    alert1 = '''
    {
        "Type": "AlarmNotification",
        "RuleName": "CPU使用率告警",
        "Level": "critical",
        "Resources": [{"InstanceId": "i-abc123"}],
        "MetricName": "CPUUtilization"
    }
    '''
    
    alert2 = '''
    {
        "Type": "AlarmNotification", 
        "RuleName": "CPU使用率告警",
        "Level": "critical",
        "Resources": [{"InstanceId": "i-abc123"}],
        "MetricName": "CPUUtilization",
        "CurrentValue": 95.5
    }
    '''
    
    compare_alerts(alert1, alert2, "cloud-monitor")
    """
    
    # 交互式输入
    print("\n请粘贴告警JSON数据（输入完成后按Ctrl+D）:")
    import sys
    alert_data = sys.stdin.read()
    if alert_data.strip():
        analyze_alert(alert_data, "unknown")
