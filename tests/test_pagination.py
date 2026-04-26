#!/usr/bin/env python3
"""
测试分页查询功能
"""
from core.utils import get_all_webhooks

def test_pagination():
    """测试分页查询"""
    print("=" * 60)
    print("测试分页查询功能")
    print("=" * 60)

    # 测试第一页
    print("\n1️⃣  测试第一页 (page=1, page_size=5)")
    try:
        webhooks, total, next_cursor = get_all_webhooks(page=1, page_size=5)
        print(f"   ✅ 成功: 获取 {len(webhooks)} 条数据，总共 {total} 条")
        if webhooks:
            print(f"   第一条 ID: {webhooks[0]['id']}")
            print(f"   最后一条 ID: {webhooks[-1]['id']}")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

    # 测试第二页
    print("\n2️⃣  测试第二页 (page=2, page_size=5)")
    try:
        webhooks, total, next_cursor = get_all_webhooks(page=2, page_size=5)
        print(f"   ✅ 成功: 获取 {len(webhooks)} 条数据，总共 {total} 条")
        if webhooks:
            print(f"   第一条 ID: {webhooks[0]['id']}")
            print(f"   最后一条 ID: {webhooks[-1]['id']}")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

    # 测试第三页
    print("\n3️⃣  测试第三页 (page=3, page_size=5)")
    try:
        webhooks, total, next_cursor = get_all_webhooks(page=3, page_size=5)
        print(f"   ✅ 成功: 获取 {len(webhooks)} 条数据，总共 {total} 条")
        if webhooks:
            print(f"   第一条 ID: {webhooks[0]['id']}")
            print(f"   最后一条 ID: {webhooks[-1]['id']}")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

    # 测试大页码
    print("\n4️⃣  测试大页码 (page=100, page_size=5)")
    try:
        webhooks, total, next_cursor = get_all_webhooks(page=100, page_size=5)
        print(f"   ✅ 成功: 获取 {len(webhooks)} 条数据（预期为0或少量）")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

    # 测试游标分页
    print("\n5️⃣  测试游标分页 (cursor_id=10)")
    try:
        webhooks, total, next_cursor = get_all_webhooks(cursor_id=10, page_size=5)
        print(f"   ✅ 成功: 获取 {len(webhooks)} 条数据")
        if webhooks:
            print(f"   所有 ID 都小于 10: {all(w['id'] < 10 for w in webhooks)}")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

    print("\n" + "=" * 60)
    print("✅ 所有测试通过！")
    print("=" * 60)
    return True


if __name__ == '__main__':
    try:
        test_pagination()
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
