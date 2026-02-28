#!/usr/bin/env python3
"""
启动时自动执行的数据库迁移检查
静默模式 - 仅在需要时输出关键信息
"""

from models import get_engine
from sqlalchemy import text
import sys


def check_and_add_unique_constraint():
    """
    检查并添加唯一约束（静默模式）

    Returns:
        bool: 成功返回True，失败返回False
    """
    engine = get_engine()

    try:
        with engine.connect() as conn:
            # 检查索引是否已存在
            result = conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE indexname = 'idx_unique_alert_hash_original'
                )
            """))
            index_exists = result.scalar()

            if index_exists:
                # 索引已存在，无需操作
                return True

            # 索引不存在，需要创建
            print("⚙️  首次启动：正在添加数据库唯一约束...")

            # 检查是否有重复的原始告警需要修复
            result = conn.execute(text("""
                SELECT COUNT(*) FROM (
                    SELECT alert_hash
                    FROM webhook_events
                    WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
                    GROUP BY alert_hash
                    HAVING COUNT(*) > 1
                ) AS duplicates
            """))
            duplicate_count = result.scalar()

            if duplicate_count > 0:
                print(f"   检测到 {duplicate_count} 组重复告警，正在修复...")

                # 获取重复数据并修复
                result = conn.execute(text("""
                    SELECT alert_hash, array_agg(id ORDER BY timestamp) as ids
                    FROM webhook_events
                    WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
                    GROUP BY alert_hash
                    HAVING COUNT(*) > 1
                """))

                duplicates = result.fetchall()
                for row in duplicates:
                    alert_hash, ids_data = row

                    # 处理不同格式的数组返回值
                    if isinstance(ids_data, list):
                        # SQLAlchemy 直接返回 Python 列表
                        ids = ids_data
                    elif isinstance(ids_data, str):
                        # 字符串格式 "{1,2,3}"
                        ids = [int(x) for x in ids_data.strip('{}').split(',')]
                    else:
                        print(f"   ⚠️  未知的数组格式: {type(ids_data)}")
                        continue

                    # 保留第一个，其他标记为重复
                    original_id = ids[0]
                    duplicate_ids = ids[1:]

                    for dup_id in duplicate_ids:
                        conn.execute(text("""
                            UPDATE webhook_events
                            SET is_duplicate = 1, duplicate_of = :original_id
                            WHERE id = :dup_id
                        """), {'original_id': original_id, 'dup_id': dup_id})

                    # 更新 duplicate_count
                    conn.execute(text("""
                        UPDATE webhook_events
                        SET duplicate_count = :count
                        WHERE id = :original_id
                    """), {'count': len(ids), 'original_id': original_id})

                conn.commit()
                print(f"   ✅ 已修复 {duplicate_count} 组重复数据")

            # 创建唯一索引
            conn.execute(text("""
                CREATE UNIQUE INDEX idx_unique_alert_hash_original
                ON webhook_events(alert_hash)
                WHERE is_duplicate = 0
            """))
            conn.commit()

            print("   ✅ 唯一约束添加成功")
            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        # 不阻止服务启动
        return False


def fix_duplicate_count():
    """
    修复重复告警的 duplicate_count 字段（静默模式）

    Returns:
        bool: 成功返回True，失败返回False
    """
    engine = get_engine()

    try:
        with engine.connect() as conn:
            # 检查是否有需要修复的记录
            result = conn.execute(text("""
                SELECT COUNT(*)
                FROM webhook_events AS duplicate_events
                JOIN webhook_events AS original_events
                    ON duplicate_events.duplicate_of = original_events.id
                WHERE duplicate_events.is_duplicate = 1
                  AND duplicate_events.duplicate_count != original_events.duplicate_count
            """))
            need_fix_count = result.scalar()

            if need_fix_count == 0:
                # 所有记录都正确，无需修复
                return True

            # 需要修复
            print(f"⚙️  检测到 {need_fix_count} 条重复告警的 duplicate_count 需要修复...")

            # 执行批量更新
            conn.execute(text("""
                UPDATE webhook_events AS duplicate_events
                SET duplicate_count = original_events.duplicate_count
                FROM webhook_events AS original_events
                WHERE duplicate_events.is_duplicate = 1
                  AND duplicate_events.duplicate_of = original_events.id
                  AND duplicate_events.duplicate_count != original_events.duplicate_count
            """))
            conn.commit()

            print(f"   ✅ 成功修复 {need_fix_count} 条记录")
            return True

    except Exception as e:
        print(f"   ⚠️  修复警告: {e}")
        # 不阻止服务启动
        return False


if __name__ == '__main__':
    success1 = check_and_add_unique_constraint()
    success2 = fix_duplicate_count()
    sys.exit(0 if (success1 and success2) else 1)
