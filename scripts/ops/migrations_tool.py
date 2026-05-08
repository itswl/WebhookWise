#!/usr/bin/env python3
"""
数据库迁移工具

使用方法:
    python migrations_tool.py add_unique_constraint
"""

import asyncio
import sys

from sqlalchemy import text

from core.logger import logger
from db.session import get_engine, init_engine, session_scope


async def add_unique_constraint(verbose=True):
    """
    添加唯一约束防止重复告警

    步骤：
    1. 修复空的 alert_hash
    2. 处理已存在的重复数据
    3. 创建唯一索引

    Args:
        verbose: 是否输出详细日志（默认True，启动脚本可设为False）
    """
    await init_engine()
    engine = get_engine()

    def log(msg, level="info"):
        """根据verbose参数决定是否输出日志"""
        if verbose:
            getattr(logger, level)(msg)

    try:
        async with session_scope() as session:
            log("🔧 开始数据库迁移：添加唯一约束...")

            # 步骤 1: 检查并修复空的 alert_hash
            log("📋 步骤 1: 检查空的 alert_hash...")
            result = await session.execute(
                text("""
                SELECT COUNT(*) FROM webhook_events
                WHERE alert_hash IS NULL AND is_duplicate = 0
            """)
            )
            null_count = result.scalar()

            if null_count > 0:
                log(f"发现 {null_count} 条 alert_hash 为空的原始告警，正在修复...", "warning")
                await session.execute(
                    text("""
                    UPDATE webhook_events
                    SET alert_hash = md5(id::text || timestamp::text)
                    WHERE alert_hash IS NULL AND is_duplicate = 0
                """)
                )
                log(f"✅ 已修复 {null_count} 条记录")
            else:
                log("✅ 无需修复，所有原始告警都有 alert_hash")

            # 步骤 2: 检查并处理重复的原始告警
            log("📋 步骤 2: 检查重复的原始告警...")
            result = await session.execute(
                text("""
                SELECT alert_hash, COUNT(*) as cnt, array_agg(id ORDER BY timestamp) as ids
                FROM webhook_events
                WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
                GROUP BY alert_hash
                HAVING COUNT(*) > 1
            """)
            )

            duplicates = result.fetchall()

            if duplicates:
                logger.warning(f"发现 {len(duplicates)} 组重复的原始告警")
                for row in duplicates:
                    alert_hash, count, ids_data = row

                    # 处理不同格式的数组返回值
                    if isinstance(ids_data, list):
                        # SQLAlchemy 直接返回 Python 列表
                        ids = ids_data
                    elif isinstance(ids_data, str):
                        # 字符串格式 "{1,2,3}"
                        ids = [int(x) for x in ids_data.strip("{}").split(",")]
                    else:
                        logger.warning(f"  未知的数组格式: {type(ids_data)}, 跳过")
                        continue

                    logger.info(f"  alert_hash={alert_hash[:16]}..., count={count}, ids={ids}")

                    # 保留最早的一条，其他标记为重复
                    original_id = ids[0]
                    duplicate_ids = ids[1:]

                    logger.info(f"  保留 ID={original_id}，将 {len(duplicate_ids)} 条标记为重复")

                    # 更新重复记录
                    for dup_id in duplicate_ids:
                        await session.execute(
                            text("""
                            UPDATE webhook_events
                            SET is_duplicate = 1, duplicate_of = :original_id
                            WHERE id = :dup_id
                        """),
                            {"original_id": original_id, "dup_id": dup_id},
                        )

                    # 更新原始告警的 duplicate_count
                    await session.execute(
                        text("""
                        UPDATE webhook_events
                        SET duplicate_count = :count
                        WHERE id = :original_id
                    """),
                        {"count": count, "original_id": original_id},
                    )

                logger.info("✅ 已处理所有重复告警")
            else:
                logger.info("✅ 无重复告警")

            # 步骤 3: 创建唯一索引（需要在事务外执行）
            logger.info("📋 步骤 3: 创建唯一索引...")

        # 使用独立连接创建索引
        async with engine.connect() as conn:
            await conn.execute(
                text("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original
                ON webhook_events(alert_hash)
                WHERE is_duplicate = 0
            """)
            )
            await conn.commit()

            logger.info("✅ 唯一索引创建成功")

            # 添加注释
            await conn.execute(
                text("""
                COMMENT ON INDEX idx_unique_alert_hash_original IS
                '确保相同 alert_hash 只有一个原始告警（is_duplicate=0），防止并发插入导致的重复'
            """)
            )
            await conn.commit()

            logger.info("✅ 注释添加成功")

        # 步骤 4: 最终验证
        logger.info("📋 步骤 4: 最终验证...")
        async with session_scope() as session:
            result = await session.execute(
                text("""
                SELECT COUNT(*) FROM webhook_events WHERE is_duplicate = 0
            """)
            )
            original_count = result.scalar()

            result = await session.execute(
                text("""
                SELECT COUNT(DISTINCT alert_hash) FROM webhook_events
                WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
            """)
            )
            unique_hash_count = result.scalar()

            logger.info(f"  原始告警总数: {original_count}")
            logger.info(f"  唯一 alert_hash 数: {unique_hash_count}")

            if original_count == unique_hash_count:
                logger.info("✅ 验证通过：每个 alert_hash 只有一个原始告警")
            else:
                logger.warning(f"⚠️  验证异常：原始告警数({original_count}) ≠ 唯一哈希数({unique_hash_count})")

        logger.info("🎉 数据库迁移完成！")
        return True

    except Exception as e:
        logger.error(f"❌ 迁移失败: {e}")
        import traceback

        traceback.print_exc()
        return False


async def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("使用方法: python migrations_tool.py <migration_name>")
        print("可用的迁移：")
        print("  - add_unique_constraint: 添加唯一约束防止重复告警")
        sys.exit(1)

    migration_name = sys.argv[1]

    if migration_name == "add_unique_constraint":
        success = await add_unique_constraint()
        sys.exit(0 if success else 1)
    else:
        print(f"未知的迁移: {migration_name}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
