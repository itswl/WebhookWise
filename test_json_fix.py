#!/usr/bin/env python3
"""测试 JSON 格式修复功能"""

import json
import sys
sys.path.insert(0, '/Users/imwl/webhooks')

from ai_analyzer import fix_json_format

# 测试用例
test_cases = [
    {
        "name": "尾随逗号",
        "input": '''{"key1": "value1", "key2": "value2",}''',
        "should_pass": True
    },
    {
        "name": "数组尾随逗号",
        "input": '''{"actions": ["action1", "action2",]}''',
        "should_pass": True
    },
    {
        "name": "单引号",
        "input": '''{'key': 'value'}''',
        "should_pass": True
    },
    {
        "name": "缺少逗号",
        "input": '''{"key1": "value1" "key2": "value2"}''',
        "should_pass": True
    },
    {
        "name": "带注释",
        "input": '''{"key": "value" // comment
}''',
        "should_pass": True
    },
    {
        "name": "多余空白",
        "input": '''  {"key": "value"}  ''',
        "should_pass": True
    },
    {
        "name": "复杂案例",
        "input": '''{
  "source": "aliyun",
  "event_type": "metric_alert",
  "importance": "medium",
  "summary": "4xx错误率略微超出阈值",
}''',
        "should_pass": True
    }
]

def test_json_fix():
    """测试 JSON 修复功能"""
    print("=" * 60)
    print("测试 JSON 格式修复功能")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for i, test in enumerate(test_cases, 1):
        print(f"\n测试 {i}: {test['name']}")
        print(f"输入: {test['input'][:100]}")
        
        try:
            # 修复 JSON
            fixed = fix_json_format(test['input'])
            print(f"修复后: {fixed[:100]}")
            
            # 尝试解析
            result = json.loads(fixed)
            print(f"✓ 解析成功: {result}")
            
            if test['should_pass']:
                passed += 1
                print("✓ 测试通过")
            else:
                failed += 1
                print("✗ 测试失败: 应该失败但成功了")
                
        except json.JSONDecodeError as e:
            print(f"✗ 解析失败: {str(e)}")
            if not test['should_pass']:
                passed += 1
                print("✓ 测试通过（预期失败）")
            else:
                failed += 1
                print("✗ 测试失败")
        except Exception as e:
            print(f"✗ 异常: {str(e)}")
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)
    
    return failed == 0

if __name__ == '__main__':
    success = test_json_fix()
    sys.exit(0 if success else 1)
