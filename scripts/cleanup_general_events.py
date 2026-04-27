#!/usr/bin/env python3
"""
清理"一般事件"告警

删除摘要中包含"一般事件:"的告警记录
警告：此操作不可逆，请先备份数据库！
"""

import os
import sys

from sqlalchemy import create_engine, text

# 必须从环境变量获取数据库连接
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")


def get_engine():
    """获取数据库引擎"""
    return create_engine(DATABASE_URL)


def preview_general_events(engine):
    """预览将要删除的"一般事件"记录"""

    # 查询统计信息
    query = text("""
        SELECT
            COUNT(*) as total_count,
            COUNT(DISTINCT alert_hash) as unique_alerts,
            MIN(timestamp) as earliest,
            MAX(timestamp) as latest,
            SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END) as original_count,
            SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) as duplicate_count
        FROM webhook_events
        WHERE ai_analysis->>'summary' LIKE '%一般事件:%'
           OR parsed_data::text LIKE '%一般事件%'
    """)

    with engine.connect() as conn:
        result = conn.execute(query)
        row = result.fetchone()

        if not row or row[0] == 0:
            print("✅ 没有找到包含'一般事件'的记录")
            return 0

        total, unique, earliest, latest, original, duplicate = row

        print("\n" + "=" * 80)
        print("📊 将要删除的'一般事件'统计：")
        print("=" * 80)
        print(f"总记录数：      {total}")
        print(f"不同告警数：    {unique}")
        print(f"原始告警：      {original}")
        print(f"重复告警：      {duplicate}")
        print(f"最早时间：      {earliest.strftime('%Y-%m-%d %H:%M:%S') if earliest else '-'}")
        print(f"最新时间：      {latest.strftime('%Y-%m-%d %H:%M:%S') if latest else '-'}")
        print("=" * 80)

        # 显示详细分类
        detail_query = text("""
            SELECT
                importance,
                source,
                COUNT(*) as count
            FROM webhook_events
            WHERE ai_analysis->>'summary' LIKE '%一般事件:%'
               OR parsed_data::text LIKE '%一般事件%'
            GROUP BY importance, source
            ORDER BY count DESC
        """)

        result = conn.execute(detail_query)
        rows = result.fetchall()

        if rows:
            print("\n按重要性和来源分类：")
            print("-" * 80)
            print(f"{'重要性':<15} {'来源':<20} {'数量':<10}")
            print("-" * 80)
            for importance, source, count in rows:
                print(f"{importance or '未设置':<15} {source or '未设置':<20} {count:<10}")
            print("-" * 80)

        # 显示示例记录
        sample_query = text("""
            SELECT
                id,
                source,
                importance,
                ai_analysis->>'summary' as summary,
                timestamp,
                is_duplicate
            FROM webhook_events
            WHERE ai_analysis->>'summary' LIKE '%一般事件:%'
               OR parsed_data::text LIKE '%一般事件%'
            ORDER BY timestamp DESC
            LIMIT 10
        """)

        result = conn.execute(sample_query)
        samples = result.fetchall()

        if samples:
            print("\n最近10条示例：")
            print("-" * 80)
            for id, source, importance, summary, ts, is_dup in samples:
                dup_mark = "[重复]" if is_dup else "[原始]"
                summary_short = (summary[:50] + '...') if summary and len(summary) > 50 else (summary or '无摘要')
                print(f"ID {id} {dup_mark} {ts.strftime('%Y-%m-%d %H:%M')}")
                print(f"  来源: {source or '未知'} | 重要性: {importance or '未设置'}")
                print(f"  摘要: {summary_short}")
                print()

        return total


def delete_general_events(engine):
    """执行删除操作"""

    delete_query = text("""
        DELETE FROM webhook_events
        WHERE ai_analysis->>'summary' LIKE '%一般事件:%'
           OR parsed_data::text LIKE '%一般事件%'
    """)

    with engine.connect() as conn:
        result = conn.execute(delete_query)
        conn.commit()
        return result.rowcount


def main():
    """主函数"""
    print("=" * 80)
    print("清理'一般事件'告警")
    print("=" * 80)
    print()
    print("删除条件：")
    print("  - ai_analysis.summary 包含 '一般事件:'")
    print("  - 或 parsed_data 包含 '一般事件'")
    print()

    engine = get_engine()

    # ============================================================
    # 预览
    # ============================================================
    print("🔍 正在扫描数据库...")
    total_count = preview_general_events(engine)

    if total_count == 0:
        print("\n没有需要清理的记录，退出。")
        return

    # ============================================================
    # 确认
    # ============================================================
    print()
    print("⚠️  警告：删除操作不可逆！")
    print("⚠️  建议先备份数据库：")
    print("     pg_dump -h <host> -U <user> -d <database> > backup.sql")
    print()

    confirm = input(f"确认删除以上 {total_count} 条'一般事件'记录？(输入 'DELETE' 确认): ")

    if confirm != 'DELETE':
        print("❌ 操作已取消")
        return

    # ============================================================
    # 执行删除
    # ============================================================
    print("\n🗑️  开始删除...")
    deleted_count = delete_general_events(engine)

    print()
    print("=" * 80)
    print(f"✅ 删除完成！共删除 {deleted_count} 条记录")
    print("=" * 80)
    print()
    print("💡 建议执行 VACUUM 回收空间：")
    print("   psql $DATABASE_URL -c 'VACUUM ANALYZE webhook_events;'")
    print()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n❌ 操作已取消")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 错误：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
