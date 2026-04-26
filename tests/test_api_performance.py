#!/usr/bin/env python3
"""
API 性能测试脚本

测试 /api/webhooks 接口在不同参数下的性能表现
"""

import time
from typing import Dict

import pytest
import requests

BASE_URL = "http://localhost:8000"

pytest.skip("performance/integration script, run manually", allow_module_level=True)


def test_api_performance(params: Dict[str, str], name: str):
    """测试 API 性能"""
    print(f"\n{'='*60}")
    print(f"测试: {name}")
    print(f"参数: {params}")
    print(f"{'='*60}")

    try:
        start_time = time.time()
        response = requests.get(f"{BASE_URL}/api/webhooks", params=params, timeout=30)
        end_time = time.time()

        elapsed_ms = (end_time - start_time) * 1000

        if response.status_code == 200:
            data = response.json()
            content_length = len(response.content)

            # 检查是否压缩
            is_compressed = response.headers.get('Content-Encoding') == 'gzip'

            print(f"✅ 成功")
            print(f"响应时间: {elapsed_ms:.2f} ms")
            print(f"响应大小: {content_length:,} 字节 ({content_length/1024:.2f} KB)")
            print(f"gzip 压缩: {'是' if is_compressed else '否'}")
            print(f"返回记录数: {len(data.get('data', []))}")
            print(f"总记录数: {data.get('pagination', {}).get('total', 0)}")

            # 分析第一条记录的字段
            if data.get('data'):
                first_record = data['data'][0]
                print(f"字段数量: {len(first_record)}")
                print(f"字段列表: {', '.join(first_record.keys())}")

                # 检查是否包含大字段
                has_raw_payload = 'raw_payload' in first_record
                has_full_ai_analysis = 'ai_analysis' in first_record
                has_summary = 'summary' in first_record

                print(f"\n字段分析:")
                print(f"  - 包含 raw_payload: {'是' if has_raw_payload else '否'}")
                print(f"  - 包含 ai_analysis: {'是' if has_full_ai_analysis else '否'}")
                print(f"  - 包含 summary: {'是' if has_summary else '否'}")
        else:
            print(f"❌ 失败: HTTP {response.status_code}")
            print(f"响应: {response.text[:200]}")

    except Exception as e:
        print(f"❌ 错误: {str(e)}")


def main():
    """主函数"""
    print("API 性能测试")
    print("="*60)

    # 测试1: 默认参数（20条，摘要模式）
    test_api_performance(
        {'page': '1', 'page_size': '20'},
        "默认参数 (20条，默认摘要模式)"
    )

    # 测试2: 100条摘要数据
    test_api_performance(
        {'page': '1', 'page_size': '100', 'fields': 'summary'},
        "100条记录 + 摘要模式"
    )

    # 测试3: 100条完整数据
    test_api_performance(
        {'page': '1', 'page_size': '100', 'fields': 'full'},
        "100条记录 + 完整模式"
    )

    # 测试4: 200条摘要数据
    test_api_performance(
        {'page': '1', 'page_size': '200', 'fields': 'summary'},
        "200条记录 + 摘要模式"
    )

    # 测试5: 尝试5000条（会被限制）
    test_api_performance(
        {'page': '1', 'page_size': '5000', 'fields': 'summary'},
        "请求5000条 (会被限制到200条) + 摘要模式"
    )

    # 测试6: 尝试5000条完整数据（会被限制）
    test_api_performance(
        {'page': '1', 'page_size': '5000', 'fields': 'full'},
        "请求5000条 (会被限制到50条) + 完整模式"
    )

    print("\n" + "="*60)
    print("测试完成")
    print("="*60)
    print("\n性能优化建议:")
    print("1. 列表页使用 fields=summary（默认）")
    print("2. 详情页使用单独 API 或 fields=full")
    print("3. 导出功能分批请求，避免一次加载太多")
    print("4. 使用游标分页（cursor）而非偏移分页提高性能")


if __name__ == "__main__":
    main()
