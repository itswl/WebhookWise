#!/usr/bin/env python3
"""
独立的 duplicate_count 修复脚本
不依赖项目其他模块，可以直接在远程服务器运行
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

# 从环境变量获取数据库连接，或使用默认值
DATABASE_URL = os.getenv(
    'DATABASE_URL',
    'postgresql://<REDACTED_DB_CREDENTIALS>@<REDACTED_DB_HOST>/webhooks'
)

def fix_duplicate_count():
    """修复所有重复告警的 duplicate_count"""
    print("=" * 60)
    print("开始修复 duplicate_count...")
    print("=" * 60)

    try:
        # 连接数据库
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. 检查需要修复的记录数
        print("\n📊 检查需要修复的记录...")
        cur.execute("""
            SELECT COUNT(*) as count
            FROM webhook_events AS duplicate_events
            JOIN webhook_events AS original_events
                ON duplicate_events.duplicate_of = original_events.id
            WHERE duplicate_events.is_duplicate = 1
              AND duplicate_events.duplicate_count != original_events.duplicate_count
        """)
        need_fix = cur.fetchone()['count']

        if need_fix == 0:
            print("✅ 所有记录已是正确的，无需修复！")
            cur.close()
            conn.close()
            return 0

        print(f"发现 {need_fix} 条记录需要修复")

        # 2. 显示前10条需要修复的记录
        print("\n前10条需要修复的记录：")
        cur.execute("""
            SELECT
                duplicate_events.id,
                duplicate_events.duplicate_of,
                duplicate_events.duplicate_count AS current_count,
                original_events.duplicate_count AS correct_count
            FROM webhook_events AS duplicate_events
            JOIN webhook_events AS original_events
                ON duplicate_events.duplicate_of = original_events.id
            WHERE duplicate_events.is_duplicate = 1
              AND duplicate_events.duplicate_count != original_events.duplicate_count
            ORDER BY duplicate_events.id
            LIMIT 10
        """)

        for row in cur.fetchall():
            print(f"  ID={row['id']}, duplicate_of={row['duplicate_of']}, "
                  f"当前值={row['current_count']}, 正确值={row['correct_count']}")

        # 3. 执行修复
        print(f"\n🔧 开始修复 {need_fix} 条记录...")
        cur.execute("""
            UPDATE webhook_events AS duplicate_events
            SET duplicate_count = original_events.duplicate_count
            FROM webhook_events AS original_events
            WHERE duplicate_events.is_duplicate = 1
              AND duplicate_events.duplicate_of = original_events.id
              AND duplicate_events.duplicate_count != original_events.duplicate_count
        """)

        conn.commit()
        print(f"✅ 成功修复 {need_fix} 条记录！")

        # 4. 验证修复结果
        print("\n📋 验证修复结果（前10条）：")
        cur.execute("""
            SELECT
                d.id,
                d.duplicate_of,
                d.duplicate_count,
                o.duplicate_count AS original_count
            FROM webhook_events d
            JOIN webhook_events o ON d.duplicate_of = o.id
            WHERE d.is_duplicate = 1
            ORDER BY d.id DESC
            LIMIT 10
        """)

        for row in cur.fetchall():
            status = "✅" if row['duplicate_count'] == row['original_count'] else "❌"
            print(f"  {status} ID={row['id']}, count={row['duplicate_count']}, "
                  f"original_count={row['original_count']}")

        cur.close()
        conn.close()

        print("\n" + "=" * 60)
        print(f"修复完成！共修复 {need_fix} 条记录")
        print("=" * 60)

        return need_fix

    except Exception as e:
        print(f"❌ 修复失败: {e}")
        import traceback
        traceback.print_exc()
        return -1


if __name__ == '__main__':
    import sys
    fixed = fix_duplicate_count()
    sys.exit(0 if fixed >= 0 else 1)
