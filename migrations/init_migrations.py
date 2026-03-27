#!/usr/bin/env python3
"""
启动时自动执行的数据库迁移检查
静默模式 - 仅在需要时输出关键信息
"""

from core.models import get_engine
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


def add_beyond_window_field():
    """
    添加 beyond_window 字段并使用链式逻辑初始化（静默模式）

    Returns:
        bool: 成功返回True，失败返回False
    """
    engine = get_engine()

    try:
        with engine.connect() as conn:
            # 检查字段是否已存在
            result = conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'webhook_events' AND column_name = 'beyond_window'
                )
            """))
            field_exists = result.scalar()

            if field_exists:
                # 字段已存在，无需操作
                return True

            # 字段不存在，需要添加
            print("⚙️  首次启动：正在添加 beyond_window 字段...")

            # 1. 添加字段
            conn.execute(text("""
                ALTER TABLE webhook_events
                ADD COLUMN beyond_window INTEGER DEFAULT 0
            """))
            conn.commit()
            print("   ✅ beyond_window 字段添加成功")

            # 2. 使用链式逻辑初始化历史数据
            print("   🔄 正在初始化历史数据的 beyond_window 值...")

            # 查询所有告警并按 hash 分组
            result = conn.execute(text("""
                SELECT id, alert_hash, timestamp, is_duplicate
                FROM webhook_events
                WHERE alert_hash IS NOT NULL
                ORDER BY alert_hash, timestamp ASC
            """))

            all_events = result.fetchall()

            # 按 alert_hash 分组
            from collections import defaultdict
            from datetime import timedelta

            hash_groups = defaultdict(list)
            for event_id, alert_hash, ts, is_dup in all_events:
                hash_groups[alert_hash].append({
                    'id': event_id,
                    'timestamp': ts,
                    'is_duplicate': is_dup
                })

            # 链式判断：基于前一条记录的时间
            time_window = timedelta(hours=24)
            update_count = 0

            for alert_hash, events in hash_groups.items():
                for i, event in enumerate(events):
                    if i == 0:
                        # 第一条记录：beyond_window = 0（原始告警）
                        beyond_value = 0
                    else:
                        # 后续记录：对比前一条的时间差
                        prev_event = events[i - 1]
                        time_diff = event['timestamp'] - prev_event['timestamp']
                        beyond_value = 1 if time_diff > time_window else 0

                    # 更新数据库
                    conn.execute(text("""
                        UPDATE webhook_events
                        SET beyond_window = :beyond_value
                        WHERE id = :event_id
                    """), {'beyond_value': beyond_value, 'event_id': event['id']})
                    update_count += 1

            conn.commit()
            print(f"   ✅ 已初始化 {update_count} 条记录的 beyond_window 值")
            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        import traceback
        traceback.print_exc()
        # 不阻止服务启动
        return False


def add_last_notified_at_field():
    """
    添加 last_notified_at 字段（静默模式）

    Returns:
        bool: 成功返回True，失败返回False
    """
    engine = get_engine()

    try:
        with engine.connect() as conn:
            # 检查字段是否已存在
            result = conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'webhook_events' AND column_name = 'last_notified_at'
                )
            """))
            field_exists = result.scalar()

            if field_exists:
                # 字段已存在，无需操作
                return True

            # 字段不存在，需要添加
            print("⚙️  首次启动：正在添加 last_notified_at 字段...")

            # 添加字段
            conn.execute(text("""
                ALTER TABLE webhook_events
                ADD COLUMN last_notified_at TIMESTAMP
            """))
            conn.commit()
            print("   ✅ last_notified_at 字段添加成功")

            # 初始化历史数据：新告警的 last_notified_at 设置为创建时间
            print("   🔄 正在初始化历史数据的 last_notified_at 值...")
            conn.execute(text("""
                UPDATE webhook_events
                SET last_notified_at = created_at
                WHERE is_duplicate = 0 AND last_notified_at IS NULL
            """))
            conn.commit()

            updated_count = conn.execute(text("""
                SELECT COUNT(*) FROM webhook_events WHERE last_notified_at IS NOT NULL
            """)).scalar()

            print(f"   ✅ 已初始化 {updated_count} 条记录的 last_notified_at 值")
            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        import traceback
        traceback.print_exc()
        # 不阻止服务启动
        return False


def add_forward_rules_table():
    """
    添加 forward_rules 转发规则表（静默模式）

    Returns:
        bool: 成功返回 True，失败返回 False
    """
    engine = get_engine()

    try:
        with engine.connect() as conn:
            # 检查表是否已存在
            result = conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'forward_rules'
                )
            """))
            table_exists = result.scalar()

            if table_exists:
                # 表已存在，无需操作
                return True

            # 表不存在，需要创建
            print("⚙️  首次启动：正在创建 forward_rules 表...")

            conn.execute(text("""
                CREATE TABLE forward_rules (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    enabled BOOLEAN DEFAULT TRUE,
                    priority INTEGER DEFAULT 0,
                    match_importance VARCHAR(50) DEFAULT '',
                    match_duplicate VARCHAR(20) DEFAULT 'all',
                    match_source VARCHAR(200) DEFAULT '',
                    target_type VARCHAR(20) NOT NULL,
                    target_url VARCHAR(500) DEFAULT '',
                    target_name VARCHAR(100) DEFAULT '',
                    stop_on_match BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """))
            conn.commit()
            print("   ✅ forward_rules 表创建成功")
            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        # 不阻止服务启动
        return False


if __name__ == '__main__':
    success1 = check_and_add_unique_constraint()
    success2 = fix_duplicate_count()
    success3 = add_beyond_window_field()
    success4 = add_last_notified_at_field()
    success5 = add_forward_rules_table()
    sys.exit(0 if (success1 and success2 and success3 and success4 and success5) else 1)
