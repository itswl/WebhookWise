#!/usr/bin/env python3
"""
添加唯一约束防止重复告警

使用方法:
    # 从环境变量读取数据库连接信息
    export DATABASE_URL="postgresql://user:pass@host:port/dbname"
    python apply_unique_constraint.py

    # 或直接指定
    DATABASE_URL="postgresql://user:pass@host:port/dbname" python apply_unique_constraint.py
"""

import os
import sys
from urllib.parse import urlparse

import psycopg2

# 从环境变量读取数据库连接 URL
DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("❌ 错误：未设置 DATABASE_URL 环境变量")
    print("使用方法：")
    print('  export DATABASE_URL="postgresql://user:pass@host:port/dbname"')
    print('  python apply_unique_constraint.py')
    sys.exit(1)

# 解析数据库 URL
try:
    parsed = urlparse(DATABASE_URL)
    DB_CONFIG = {
        'host': parsed.hostname,
        'port': parsed.port or 5432,
        'user': parsed.username,
        'password': parsed.password,
        'database': parsed.path.lstrip('/')
    }
except Exception as e:
    print(f"❌ 错误：无法解析 DATABASE_URL: {e}")
    sys.exit(1)


def apply_migration():
    """应用数据库迁移"""
    print("🔧 开始应用数据库迁移...")

    try:
        # 连接数据库
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        cursor = conn.cursor()

        print("✅ 数据库连接成功")

        # 1. 检查并修复空的 alert_hash
        print("\n📋 步骤 1: 检查空的 alert_hash...")
        cursor.execute("""
            SELECT COUNT(*) FROM webhook_events
            WHERE alert_hash IS NULL AND is_duplicate = 0
        """)
        null_count = cursor.fetchone()[0]

        if null_count > 0:
            print(f"⚠️  发现 {null_count} 条 alert_hash 为空的原始告警，正在修复...")
            cursor.execute("""
                UPDATE webhook_events
                SET alert_hash = md5(id::text || timestamp::text)
                WHERE alert_hash IS NULL AND is_duplicate = 0
            """)
            print(f"✅ 已修复 {null_count} 条记录")
        else:
            print("✅ 无需修复，所有原始告警都有 alert_hash")

        # 2. 检查是否存在重复的原始告警
        print("\n📋 步骤 2: 检查重复的原始告警...")
        cursor.execute("""
            SELECT alert_hash, COUNT(*) as cnt, array_agg(id ORDER BY timestamp) as ids
            FROM webhook_events
            WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
            GROUP BY alert_hash
            HAVING COUNT(*) > 1
        """)

        duplicates = cursor.fetchall()

        if duplicates:
            print(f"⚠️  发现 {len(duplicates)} 组重复的原始告警：")
            for alert_hash, count, ids in duplicates:
                print(f"   - alert_hash={alert_hash[:16]}..., count={count}, ids={ids}")

                # 保留最早的一条，其他标记为重复
                original_id = ids[0]
                duplicate_ids = ids[1:]

                print(f"   保留 ID={original_id}，将 {duplicate_ids} 标记为重复")

                cursor.execute("""
                    UPDATE webhook_events
                    SET is_duplicate = 1,
                        duplicate_of = %s
                    WHERE id = ANY(%s)
                """, (original_id, duplicate_ids))

                # 更新原始告警的 duplicate_count
                cursor.execute("""
                    UPDATE webhook_events
                    SET duplicate_count = %s
                    WHERE id = %s
                """, (count, original_id))

            print("✅ 已处理所有重复告警")
        else:
            print("✅ 无重复告警")

        # 3. 创建唯一索引
        print("\n📋 步骤 3: 创建唯一索引...")
        try:
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original
                ON webhook_events(alert_hash)
                WHERE is_duplicate = 0
            """)
            print("✅ 唯一索引创建成功")
        except psycopg2.errors.UniqueViolation as e:
            print(f"❌ 创建索引失败（仍有重复数据）: {e}")
            return False

        # 4. 添加注释
        print("\n📋 步骤 4: 添加索引注释...")
        cursor.execute("""
            COMMENT ON INDEX idx_unique_alert_hash_original IS
            '确保相同 alert_hash 只有一个原始告警（is_duplicate=0），防止并发插入导致的重复'
        """)
        print("✅ 注释添加成功")

        # 5. 最终验证
        print("\n📋 步骤 5: 最终验证...")
        cursor.execute("""
            SELECT COUNT(*) FROM webhook_events
            WHERE is_duplicate = 0
        """)
        original_count = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(DISTINCT alert_hash) FROM webhook_events
            WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
        """)
        unique_hash_count = cursor.fetchone()[0]

        print(f"   原始告警总数: {original_count}")
        print(f"   唯一 alert_hash 数: {unique_hash_count}")

        if original_count == unique_hash_count:
            print("✅ 验证通过：每个 alert_hash 只有一个原始告警")
        else:
            print(f"⚠️  验证异常：原始告警数({original_count}) ≠ 唯一哈希数({unique_hash_count})")

        cursor.close()
        conn.close()

        print("\n🎉 数据库迁移完成！")
        return True

    except Exception as e:
        print(f"\n❌ 迁移失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    success = apply_migration()
    sys.exit(0 if success else 1)
