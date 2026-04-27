#!/usr/bin/env python3
"""
修复重复告警的 duplicate_count 字段

问题：重复告警记录的 duplicate_count 都是 1，应该继承原始告警的累计次数
解决：将重复告警的 duplicate_count 更新为原始告警的 duplicate_count
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from core.logger import logger
from db.session import init_db, session_scope


def fix_duplicate_count():
    """修复所有重复告警的 duplicate_count 字段"""
    try:
        # 初始化数据库
        init_db()

        with session_scope() as session:
            # 查询需要修复的记录
            query = text("""
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
            """)

            result = session.execute(query)
            rows = result.fetchall()

            if not rows:
                logger.info("✅ 所有重复告警的 duplicate_count 已是正确的，无需修复")
                return 0

            logger.info(f"📊 发现 {len(rows)} 条重复告警需要修复 duplicate_count")

            # 显示前10条示例
            logger.info("前10条需要修复的记录：")
            for row in rows[:10]:
                logger.info(f"  ID={row[0]}, duplicate_of={row[1]}, "
                          f"当前值={row[2]}, 正确值={row[3]}")

            # 执行批量更新
            update_query = text("""
                UPDATE webhook_events AS duplicate_events
                SET duplicate_count = original_events.duplicate_count
                FROM webhook_events AS original_events
                WHERE duplicate_events.is_duplicate = 1
                  AND duplicate_events.duplicate_of = original_events.id
                  AND duplicate_events.duplicate_count != original_events.duplicate_count
            """)

            session.execute(update_query)
            session.commit()

            logger.info(f"✅ 成功修复 {len(rows)} 条重复告警记录")
            return len(rows)

    except Exception as e:
        logger.error(f"❌ 修复失败: {e!s}", exc_info=True)
        raise


if __name__ == '__main__':
    fixed_count = fix_duplicate_count()
    print(f"\n{'='*60}")
    print(f"修复完成！共修复 {fixed_count} 条记录")
    print(f"{'='*60}\n")
