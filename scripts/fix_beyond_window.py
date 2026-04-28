#!/usr/bin/env python3
"""
修复 beyond_window 字段 - 使用链式判断逻辑

问题：之前的逻辑追溯到原始告警判断窗口，导致后续重复告警都被标记为窗口外
修复：基于最近的重复告警时间判断，形成链式关系

场景：
- 原始告警(ID=5, 51天前)
- ID=8983(刚来) → 窗口外 ✓
- ID=8984(刚来) → 窗口内 (最近的是8983) ✓
- ID=8985(刚来) → 窗口内 (最近的是8984) ✓
"""

import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text


def get_engine():
    """获取数据库引擎"""
    # 必须使用环境变量 DATABASE_URL
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is required")
    return create_engine(db_url)


def fix_beyond_window(time_window_hours=24):
    """
    修复所有重复告警的 beyond_window 字段

    Args:
        time_window_hours: 时间窗口（小时），默认24小时

    Returns:
        int: 修复的记录数
    """
    engine = get_engine()

    try:
        with engine.connect() as conn:
            # 1. 查询所有需要检查的重复告警（按 alert_hash 和时间排序）
            print("📊 分析需要修复的告警...")

            result = conn.execute(
                text("""
                SELECT
                    id,
                    alert_hash,
                    is_duplicate,
                    duplicate_of,
                    beyond_window AS current_beyond_window,
                    timestamp
                FROM webhook_events
                WHERE alert_hash IS NOT NULL
                ORDER BY alert_hash, timestamp ASC
            """)
            )

            all_events = result.fetchall()

            # 2. 按 alert_hash 分组
            hash_groups = {}
            for event in all_events:
                event_id, alert_hash, is_dup, dup_of, current_beyond, ts = event
                if alert_hash not in hash_groups:
                    hash_groups[alert_hash] = []
                hash_groups[alert_hash].append(
                    {
                        "id": event_id,
                        "is_duplicate": is_dup,
                        "duplicate_of": dup_of,
                        "current_beyond_window": current_beyond,
                        "timestamp": ts,
                    }
                )

            print(f"   找到 {len(hash_groups)} 个不同的告警链")

            # 3. 对每个 alert_hash 链，重新计算 beyond_window
            updates = []
            now = datetime.now()
            time_threshold = timedelta(hours=time_window_hours)

            for events in hash_groups.values():
                # 第一个事件（最早的原始告警）
                if len(events) == 1:
                    continue  # 只有一条记录，不需要修复

                for i, event in enumerate(events):
                    if i == 0:
                        # 第一个告警（原始告警）
                        # beyond_window 应该基于当前时间判断
                        time_diff = now - event["timestamp"]
                        new_beyond_window = time_diff > time_threshold
                    else:
                        # 后续重复告警
                        # 链式判断：看上一个告警是否在窗口内（相对于当前记录的时间）
                        prev_event = events[i - 1]
                        time_diff = event["timestamp"] - prev_event["timestamp"]

                        # 如果距离上一个告警超过窗口，则为窗口外
                        new_beyond_window = time_diff > time_threshold

                    # 检查是否需要更新
                    if event["current_beyond_window"] != new_beyond_window:
                        updates.append(
                            {
                                "id": event["id"],
                                "old_value": event["current_beyond_window"],
                                "new_value": new_beyond_window,
                                "timestamp": event["timestamp"],
                            }
                        )

            if not updates:
                print("✅ 所有记录的 beyond_window 字段都正确，无需修复")
                return 0

            print(f"\n📋 需要修复 {len(updates)} 条记录")
            print("=" * 80)
            print(f"{'ID':<8} {'旧值':<12} {'新值':<12} {'时间'}")
            print("=" * 80)

            for update in updates[:20]:  # 只显示前20条
                old = "窗口外" if update["old_value"] else "窗口内"
                new = "窗口外" if update["new_value"] else "窗口内"
                print(f"{update['id']:<8} {old:<12} {new:<12} {update['timestamp']}")

            if len(updates) > 20:
                print(f"... 还有 {len(updates) - 20} 条")

            # 4. 批量更新
            print("\n🔧 开始批量更新...")

            for update in updates:
                conn.execute(
                    text("""
                    UPDATE webhook_events
                    SET beyond_window = :new_value
                    WHERE id = :event_id
                """),
                    {"new_value": update["new_value"], "event_id": update["id"]},
                )

            conn.commit()

            print(f"✅ 成功修复 {len(updates)} 条记录")
            return len(updates)

    except Exception as e:
        print(f"❌ 修复失败: {e}")
        import traceback

        traceback.print_exc()
        return 0


if __name__ == "__main__":
    print("=" * 80)
    print("修复 beyond_window 字段 - 使用链式判断逻辑")
    print("=" * 80)
    print()

    fixed_count = fix_beyond_window()

    print()
    print("=" * 80)
    print(f"修复完成！共修复 {fixed_count} 条记录")
    print("=" * 80)

    sys.exit(0 if fixed_count >= 0 else 1)
