#!/usr/bin/env python3
"""
检查数据库中告警的 importance 字段分布
"""
from models import WebhookEvent, session_scope

def check_importance_distribution():
    """检查 importance 字段分布"""
    with session_scope() as session:
        # 查询所有 webhooks
        webhooks = session.query(WebhookEvent).order_by(WebhookEvent.id.desc()).limit(100).all()

        print(f"检查最近 {len(webhooks)} 条告警的 importance 字段分布\n")

        # 统计分布
        stats = {
            'high': 0,
            'medium': 0,
            'low': 0,
            'null': 0,
            'empty': 0,
            'other': []
        }

        for webhook in webhooks:
            imp = webhook.importance

            if imp is None:
                stats['null'] += 1
            elif imp == '':
                stats['empty'] += 1
            elif imp == 'high':
                stats['high'] += 1
            elif imp == 'medium':
                stats['medium'] += 1
            elif imp == 'low':
                stats['low'] += 1
            else:
                stats['other'].append(imp)

        # 打印统计结果
        print("=" * 60)
        print("优先级分布统计")
        print("=" * 60)
        print(f"高优先级 (high):     {stats['high']:3d} 条")
        print(f"中优先级 (medium):   {stats['medium']:3d} 条")
        print(f"低优先级 (low):      {stats['low']:3d} 条")
        print(f"空值 (null):         {stats['null']:3d} 条")
        print(f"空字符串 (''):       {stats['empty']:3d} 条")

        if stats['other']:
            print(f"其他值:              {len(stats['other']):3d} 条")
            print(f"  → {set(stats['other'])}")

        print("=" * 60)

        # 显示前 10 条的详细信息
        print("\n前 10 条告警详情:")
        print("=" * 60)
        print(f"{'ID':<6} {'Importance':<12} {'Source':<20} {'Is Duplicate'}")
        print("-" * 60)

        for webhook in webhooks[:10]:
            imp = webhook.importance or 'null'
            source = (webhook.source or 'unknown')[:20]
            is_dup = 'Yes' if webhook.is_duplicate else 'No'
            print(f"{webhook.id:<6} {imp:<12} {source:<20} {is_dup}")

        print("=" * 60)

        # 检查是否需要更新
        null_count = stats['null'] + stats['empty']
        if null_count > 0:
            print(f"\n⚠️  发现 {null_count} 条告警的 importance 为空")
            print("这些告警在前端会被默认显示为'低优先级'")
            print("\n如果需要更新这些记录，可以运行以下 SQL:")
            print("UPDATE webhook_events SET importance = 'low' WHERE importance IS NULL OR importance = '';")
        else:
            print("\n✅ 所有告警都有正确的 importance 值")

if __name__ == '__main__':
    try:
        check_importance_distribution()
    except Exception as e:
        print(f"错误: {e}")
        print("\n提示: 请确保数据库连接正常")
        print("运行: python -c \"from models import test_db_connection; test_db_connection()\"")
