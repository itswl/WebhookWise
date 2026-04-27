#!/usr/bin/env python3
"""
批量清理告警脚本

用途：删除低价值告警，清理数据库
警告：此操作不可逆，请先备份数据库！
"""

import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text

# 必须从环境变量获取数据库连接
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")


def get_engine():
    """获取数据库引擎"""
    return create_engine(DATABASE_URL)


def preview_cleanup(engine, **filters):
    """
    预览将要删除的记录

    filters 参数示例：
        importance: 'low' 或 'medium'
        source: 'unknown'
        before_date: datetime 对象
        keep_recent_days: 保留最近N天
    """
    conditions = []
    params = {}

    # 重要性过滤
    if 'importance' in filters:
        if isinstance(filters['importance'], list):
            placeholders = ','.join([f":imp_{i}" for i in range(len(filters['importance']))])
            conditions.append(f"importance IN ({placeholders})")
            for i, imp in enumerate(filters['importance']):
                params[f'imp_{i}'] = imp
        else:
            conditions.append("importance = :importance")
            params['importance'] = filters['importance']

    # 来源过滤
    if 'source' in filters:
        conditions.append("source = :source")
        params['source'] = filters['source']

    # 时间过滤
    if 'before_date' in filters:
        conditions.append("timestamp < :before_date")
        params['before_date'] = filters['before_date']

    if 'keep_recent_days' in filters:
        cutoff_date = datetime.now() - timedelta(days=filters['keep_recent_days'])
        conditions.append("timestamp < :cutoff_date")
        params['cutoff_date'] = cutoff_date

    # 构建查询
    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = text(f"""
        SELECT
            importance,
            source,
            COUNT(*) as count,
            MIN(timestamp) as earliest,
            MAX(timestamp) as latest
        FROM webhook_events
        WHERE {where_clause}
        GROUP BY importance, source
        ORDER BY count DESC
    """)

    with engine.connect() as conn:
        result = conn.execute(query, params)
        rows = result.fetchall()

        if not rows:
            print("✅ 没有符合条件的记录")
            return 0

        print("\n" + "=" * 80)
        print("将要删除的记录统计：")
        print("=" * 80)
        print(f"{'重要性':<12} {'来源':<15} {'数量':<10} {'最早':<20} {'最新':<20}")
        print("-" * 80)

        total = 0
        for row in rows:
            importance, source, count, earliest, latest = row
            total += count
            earliest_str = earliest.strftime('%Y-%m-%d %H:%M') if earliest else '-'
            latest_str = latest.strftime('%Y-%m-%d %H:%M') if latest else '-'
            print(f"{importance:<12} {source:<15} {count:<10} {earliest_str:<20} {latest_str:<20}")

        print("-" * 80)
        print(f"总计：{total} 条记录")
        print("=" * 80)

        return total


def delete_alerts(engine, **filters):
    """
    执行删除操作

    返回：删除的记录数
    """
    conditions = []
    params = {}

    # 重要性过滤
    if 'importance' in filters:
        if isinstance(filters['importance'], list):
            placeholders = ','.join([f":imp_{i}" for i in range(len(filters['importance']))])
            conditions.append(f"importance IN ({placeholders})")
            for i, imp in enumerate(filters['importance']):
                params[f'imp_{i}'] = imp
        else:
            conditions.append("importance = :importance")
            params['importance'] = filters['importance']

    # 来源过滤
    if 'source' in filters:
        conditions.append("source = :source")
        params['source'] = filters['source']

    # 时间过滤
    if 'before_date' in filters:
        conditions.append("timestamp < :before_date")
        params['before_date'] = filters['before_date']

    if 'keep_recent_days' in filters:
        cutoff_date = datetime.now() - timedelta(days=filters['keep_recent_days'])
        conditions.append("timestamp < :cutoff_date")
        params['cutoff_date'] = cutoff_date

    # 构建删除语句
    where_clause = " AND ".join(conditions) if conditions else "1=1"

    delete_query = text(f"""
        DELETE FROM webhook_events
        WHERE {where_clause}
    """)

    with engine.connect() as conn:
        result = conn.execute(delete_query, params)
        conn.commit()
        return result.rowcount


def main():
    """主函数"""
    print("=" * 80)
    print("批量清理告警脚本")
    print("=" * 80)
    print()

    engine = get_engine()

    # ============================================================
    # 配置清理规则（根据需求修改）
    # ============================================================

    # 示例1：删除所有 importance=low 的告警
    # filters = {'importance': 'low'}

    # 示例2：删除 importance=low 或 medium 的告警
    # filters = {'importance': ['low', 'medium']}

    # 示例3：删除来源为 unknown 的告警
    # filters = {'source': 'unknown'}

    # 示例4：删除30天前的 low 告警
    # filters = {'importance': 'low', 'keep_recent_days': 30}

    # 示例5：删除来源为 unknown 且重要性为 low/medium 的告警
    # filters = {
    #     'source': 'unknown',
    #     'importance': ['low', 'medium']
    # }

    # 默认配置：删除来源为 unknown 且重要性为 medium 或 low 的告警
    filters = {
        'source': 'unknown',
        'importance': ['medium', 'low']
    }

    print("当前清理规则：")
    for key, value in filters.items():
        print(f"  - {key}: {value}")
    print()

    # ============================================================
    # 预览
    # ============================================================
    print("🔍 预览将要删除的记录...")
    total_count = preview_cleanup(engine, **filters)

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

    confirm = input(f"确认删除以上 {total_count} 条记录？(输入 'yes' 确认，其他取消): ")

    if confirm.lower() != 'yes':
        print("❌ 操作已取消")
        return

    # ============================================================
    # 执行删除
    # ============================================================
    print("\n🗑️  开始删除...")
    deleted_count = delete_alerts(engine, **filters)

    print()
    print("=" * 80)
    print(f"✅ 删除完成！共删除 {deleted_count} 条记录")
    print("=" * 80)


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
