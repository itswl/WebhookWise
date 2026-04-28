#!/usr/bin/env python3
"""
启动时自动执行的数据库迁移检查
静默模式 - 仅在需要时输出关键信息
"""

import sys

from sqlalchemy import text

from db.session import get_sync_engine


def check_and_add_unique_constraint():
    """
    检查并添加唯一约束（静默模式）

    Returns:
        bool: 成功返回True，失败返回False
    """
    engine = get_sync_engine()

    try:
        with engine.connect() as conn:
            # 检查索引是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE indexname = 'idx_unique_alert_hash_original'
                )
            """)
            )
            index_exists = result.scalar()

            if index_exists:
                # 索引已存在，无需操作
                return True

            # 索引不存在，需要创建
            print("⚙️  首次启动：正在添加数据库唯一约束...")

            # 检查是否有重复的原始告警需要修复
            result = conn.execute(
                text("""
                SELECT COUNT(*) FROM (
                    SELECT alert_hash
                    FROM webhook_events
                    WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
                    GROUP BY alert_hash
                    HAVING COUNT(*) > 1
                ) AS duplicates
            """)
            )
            duplicate_count = result.scalar()

            if duplicate_count > 0:
                print(f"   检测到 {duplicate_count} 组重复告警，正在修复...")

                # 获取重复数据并修复
                result = conn.execute(
                    text("""
                    SELECT alert_hash, array_agg(id ORDER BY timestamp) as ids
                    FROM webhook_events
                    WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
                    GROUP BY alert_hash
                    HAVING COUNT(*) > 1
                """)
                )

                duplicates = result.fetchall()
                for row in duplicates:
                    _alert_hash, ids_data = row

                    # 处理不同格式的数组返回值
                    if isinstance(ids_data, list):
                        # SQLAlchemy 直接返回 Python 列表
                        ids = ids_data
                    elif isinstance(ids_data, str):
                        # 字符串格式 "{1,2,3}"
                        ids = [int(x) for x in ids_data.strip("{}").split(",")]
                    else:
                        print(f"   ⚠️  未知的数组格式: {type(ids_data)}")
                        continue

                    # 保留第一个，其他标记为重复
                    original_id = ids[0]
                    duplicate_ids = ids[1:]

                    for dup_id in duplicate_ids:
                        conn.execute(
                            text("""
                            UPDATE webhook_events
                            SET is_duplicate = 1, duplicate_of = :original_id
                            WHERE id = :dup_id
                        """),
                            {"original_id": original_id, "dup_id": dup_id},
                        )

                    # 更新 duplicate_count
                    conn.execute(
                        text("""
                        UPDATE webhook_events
                        SET duplicate_count = :count
                        WHERE id = :original_id
                    """),
                        {"count": len(ids), "original_id": original_id},
                    )

                conn.commit()
                print(f"   ✅ 已修复 {duplicate_count} 组重复数据")

            # 创建唯一索引
            conn.execute(
                text("""
                CREATE UNIQUE INDEX idx_unique_alert_hash_original
                ON webhook_events(alert_hash)
                WHERE is_duplicate = 0
            """)
            )
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
    engine = get_sync_engine()

    try:
        with engine.connect() as conn:
            # 检查是否有需要修复的记录
            result = conn.execute(
                text("""
                SELECT COUNT(*)
                FROM webhook_events AS duplicate_events
                JOIN webhook_events AS original_events
                    ON duplicate_events.duplicate_of = original_events.id
                WHERE duplicate_events.is_duplicate = 1
                  AND duplicate_events.duplicate_count != original_events.duplicate_count
            """)
            )
            need_fix_count = result.scalar()

            if need_fix_count == 0:
                # 所有记录都正确，无需修复
                return True

            # 需要修复
            print(f"⚙️  检测到 {need_fix_count} 条重复告警的 duplicate_count 需要修复...")

            # 执行批量更新
            conn.execute(
                text("""
                UPDATE webhook_events AS duplicate_events
                SET duplicate_count = original_events.duplicate_count
                FROM webhook_events AS original_events
                WHERE duplicate_events.is_duplicate = 1
                  AND duplicate_events.duplicate_of = original_events.id
                  AND duplicate_events.duplicate_count != original_events.duplicate_count
            """)
            )
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
    engine = get_sync_engine()

    try:
        with engine.connect() as conn:
            # 检查字段是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'webhook_events' AND column_name = 'beyond_window'
                )
            """)
            )
            field_exists = result.scalar()

            if field_exists:
                # 字段已存在，无需操作
                return True

            # 字段不存在，需要添加
            print("⚙️  首次启动：正在添加 beyond_window 字段...")

            # 1. 添加字段
            conn.execute(
                text("""
                ALTER TABLE webhook_events
                ADD COLUMN beyond_window INTEGER DEFAULT 0
            """)
            )
            conn.commit()
            print("   ✅ beyond_window 字段添加成功")

            # 2. 使用链式逻辑初始化历史数据
            print("   🔄 正在初始化历史数据的 beyond_window 值...")

            # 查询所有告警并按 hash 分组
            result = conn.execute(
                text("""
                SELECT id, alert_hash, timestamp, is_duplicate
                FROM webhook_events
                WHERE alert_hash IS NOT NULL
                ORDER BY alert_hash, timestamp ASC
            """)
            )

            all_events = result.fetchall()

            # 按 alert_hash 分组
            from collections import defaultdict
            from datetime import timedelta

            hash_groups = defaultdict(list)
            for event_id, alert_hash, ts, is_dup in all_events:
                hash_groups[alert_hash].append({"id": event_id, "timestamp": ts, "is_duplicate": is_dup})

            # 链式判断：基于前一条记录的时间
            time_window = timedelta(hours=24)
            update_count = 0

            for events in hash_groups.values():
                for i, event in enumerate(events):
                    if i == 0:
                        # 第一条记录：beyond_window = 0（原始告警）
                        beyond_value = 0
                    else:
                        # 后续记录：对比前一条的时间差
                        prev_event = events[i - 1]
                        time_diff = event["timestamp"] - prev_event["timestamp"]
                        beyond_value = 1 if time_diff > time_window else 0

                    # 更新数据库
                    conn.execute(
                        text("""
                        UPDATE webhook_events
                        SET beyond_window = :beyond_value
                        WHERE id = :event_id
                    """),
                        {"beyond_value": beyond_value, "event_id": event["id"]},
                    )
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
    engine = get_sync_engine()

    try:
        with engine.connect() as conn:
            # 检查字段是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'webhook_events' AND column_name = 'last_notified_at'
                )
            """)
            )
            field_exists = result.scalar()

            if field_exists:
                # 字段已存在，无需操作
                return True

            # 字段不存在，需要添加
            print("⚙️  首次启动：正在添加 last_notified_at 字段...")

            # 添加字段
            conn.execute(
                text("""
                ALTER TABLE webhook_events
                ADD COLUMN last_notified_at TIMESTAMP
            """)
            )
            conn.commit()
            print("   ✅ last_notified_at 字段添加成功")

            # 初始化历史数据：新告警的 last_notified_at 设置为创建时间
            print("   🔄 正在初始化历史数据的 last_notified_at 值...")
            conn.execute(
                text("""
                UPDATE webhook_events
                SET last_notified_at = created_at
                WHERE is_duplicate = 0 AND last_notified_at IS NULL
            """)
            )
            conn.commit()

            updated_count = conn.execute(
                text("""
                SELECT COUNT(*) FROM webhook_events WHERE last_notified_at IS NOT NULL
            """)
            ).scalar()

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
    engine = get_sync_engine()

    try:
        with engine.connect() as conn:
            # 检查表是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'forward_rules'
                )
            """)
            )
            table_exists = result.scalar()

            if table_exists:
                # 表已存在，无需操作
                return True

            # 表不存在，需要创建
            print("⚙️  首次启动：正在创建 forward_rules 表...")

            conn.execute(
                text("""
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
            """)
            )
            conn.commit()
            print("   ✅ forward_rules 表创建成功")
            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        # 不阻止服务启动
        return False


def add_deep_analyses_table():
    """
    添加 deep_analyses 深度分析历史表（静默模式）

    Returns:
        bool: 成功返回 True，失败返回 False
    """
    engine = get_sync_engine()

    try:
        with engine.connect() as conn:
            # 检查表是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'deep_analyses'
                )
            """)
            )
            table_exists = result.scalar()

            if table_exists:
                # 表已存在，无需操作
                return True

            # 表不存在，需要创建
            print("⚙️  首次启动：正在创建 deep_analyses 表...")

            conn.execute(
                text("""
                CREATE TABLE deep_analyses (
                    id SERIAL PRIMARY KEY,
                    webhook_event_id INTEGER NOT NULL,
                    engine VARCHAR(20) DEFAULT 'local',
                    user_question TEXT DEFAULT '',
                    analysis_result JSON,
                    duration_seconds FLOAT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            )
            conn.execute(
                text("""
                CREATE INDEX idx_deep_analyses_webhook_event_id ON deep_analyses(webhook_event_id)
            """)
            )
            conn.commit()
            print("   ✅ deep_analyses 表创建成功")
            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        # 不阻止服务启动
        return False


def add_polling_fields():
    """
    为 deep_analyses 表添加轮询相关字段（静默模式）

    Returns:
        bool: 成功返回 True，失败返回 False
    """
    engine = get_sync_engine()

    try:
        with engine.connect() as conn:
            # 检查 openclaw_run_id 字段是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'deep_analyses' AND column_name = 'openclaw_run_id'
                )
            """)
            )
            field_exists = result.scalar()

            if field_exists:
                return True

            print("⚙️  正在为 deep_analyses 表添加轮询字段...")

            conn.execute(text("ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS openclaw_run_id VARCHAR(64)"))
            conn.execute(text("ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS openclaw_session_key VARCHAR(200)"))
            conn.execute(
                text("ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'completed'")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS idx_deep_analyses_openclaw_run_id ON deep_analyses(openclaw_run_id)")
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_deep_analyses_status ON deep_analyses(status)"))
            conn.commit()

            print("   ✅ deep_analyses 轮询字段添加成功")
            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        return False


def add_archive_and_indexes():
    """
    执行归档表创建和复合索引优化
    """
    engine = get_sync_engine()
    from pathlib import Path

    sql_path = Path(__file__).parent / "sql" / "archive_and_index.sql"
    if not sql_path.exists():
        return True

    try:
        with engine.connect() as conn:
            # 检查其中一个新索引是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE indexname = 'idx_webhook_hash_timestamp'
                )
            """)
            )
            if result.scalar():
                return True

            print("⚙️  正在执行数据库性能优化 (复合索引与归档表)...")

            with open(sql_path) as f:
                sql_content = f.read()

            # 执行 SQL 脚本
            conn.execute(text(sql_content))
            conn.commit()

            print("   ✅ 数据库性能优化脚本执行成功")
            return True

    except Exception as e:
        print(f"   ⚠️  性能优化迁移警告: {e}")
        return False


def add_failed_forwards_table():
    """
    添加 failed_forwards 转发失败记录表（静默模式）

    Returns:
        bool: 成功返回 True，失败返回 False
    """
    engine = get_sync_engine()

    try:
        with engine.connect() as conn:
            # 检查表是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'failed_forwards'
                )
            """)
            )
            table_exists = result.scalar()

            if table_exists:
                # 表已存在，无需操作
                return True

            # 表不存在，需要创建
            print("⚙️  首次启动：正在创建 failed_forwards 表...")

            conn.execute(
                text("""
                CREATE TABLE failed_forwards (
                    id SERIAL PRIMARY KEY,
                    webhook_event_id INTEGER NOT NULL,
                    forward_rule_id INTEGER,
                    target_url VARCHAR(500) NOT NULL,
                    target_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending',
                    failure_reason VARCHAR(500),
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    next_retry_at TIMESTAMP,
                    last_retry_at TIMESTAMP,
                    forward_data JSON,
                    forward_headers JSON,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            )

            # 创建索引
            conn.execute(
                text("""
                CREATE INDEX idx_failed_status_retry ON failed_forwards(status, next_retry_at)
            """)
            )
            conn.execute(
                text("""
                CREATE INDEX idx_failed_webhook_event ON failed_forwards(webhook_event_id)
            """)
            )
            conn.commit()
            print("   ✅ failed_forwards 表创建成功")
            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        # 不阻止服务启动
        return False


def add_system_configs_table():
    """
    添加 system_configs 运行时配置表（静默模式）

    Returns:
        bool: 成功返回 True，失败返回 False
    """
    engine = get_sync_engine()

    # 需要从 Config 读取当前值作为 seed
    from core.config import Config

    _RUNTIME_CONFIG_SEED = {
        "FORWARD_URL": {"type": "str", "desc": "告警转发目标 URL"},
        "ENABLE_FORWARD": {"type": "bool", "desc": "启用告警转发"},
        "ENABLE_AI_ANALYSIS": {"type": "bool", "desc": "启用 AI 分析"},
        "OPENAI_API_KEY": {"type": "str", "desc": "OpenAI API 密钥"},
        "OPENAI_API_URL": {"type": "str", "desc": "OpenAI API 地址"},
        "OPENAI_MODEL": {"type": "str", "desc": "AI 模型名称"},
        "AI_SYSTEM_PROMPT": {"type": "str", "desc": "AI 系统提示词"},
        "LOG_LEVEL": {"type": "str", "desc": "日志级别"},
        "DUPLICATE_ALERT_TIME_WINDOW": {"type": "int", "desc": "告警去重时间窗口（小时）"},
        "FORWARD_DUPLICATE_ALERTS": {"type": "bool", "desc": "转发重复告警"},
        "REANALYZE_AFTER_TIME_WINDOW": {"type": "bool", "desc": "超窗口后重新分析"},
        "FORWARD_AFTER_TIME_WINDOW": {"type": "bool", "desc": "超窗口后转发"},
        "ENABLE_ALERT_NOISE_REDUCTION": {"type": "bool", "desc": "启用智能降噪"},
        "NOISE_REDUCTION_WINDOW_MINUTES": {"type": "int", "desc": "降噪时间窗口（分钟）"},
        "ROOT_CAUSE_MIN_CONFIDENCE": {"type": "float", "desc": "根因关联最小置信度"},
        "SUPPRESS_DERIVED_ALERT_FORWARD": {"type": "bool", "desc": "抑制衍生告警转发"},
    }

    try:
        with engine.connect() as conn:
            # 检查表是否已存在
            result = conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'system_configs'
                )
            """)
            )
            table_exists = result.scalar()

            if not table_exists:
                # 表不存在，需要创建
                print("⚙️  首次启动：正在创建 system_configs 表...")

                conn.execute(
                    text("""
                    CREATE TABLE system_configs (
                        key VARCHAR(128) PRIMARY KEY,
                        value TEXT NOT NULL,
                        value_type VARCHAR(16) NOT NULL DEFAULT 'str',
                        description TEXT,
                        updated_at TIMESTAMP DEFAULT NOW(),
                        updated_by VARCHAR(64) DEFAULT 'system'
                    )
                """)
                )
                conn.commit()
                print("   ✅ system_configs 表创建成功")

            # 检查表是否为空，若为空则 seed 初始配置
            result = conn.execute(text("SELECT COUNT(*) FROM system_configs"))
            row_count = result.scalar()

            if row_count == 0:
                print("🔄 正在初始化运行时配置种子数据...")
                for key, meta in _RUNTIME_CONFIG_SEED.items():
                    val = getattr(Config, key, "")
                    # 布尔值特殊处理
                    str_val = str(val).lower() if meta["type"] == "bool" else str(val) if val is not None else ""
                    conn.execute(
                        text("""
                        INSERT INTO system_configs (key, value, value_type, description, updated_by)
                        VALUES (:key, :value, :value_type, :description, 'migration')
                    """),
                        {
                            "key": key,
                            "value": str_val,
                            "value_type": meta["type"],
                            "description": meta["desc"],
                        },
                    )
                conn.commit()
                print(f"   ✅ 已初始化 {len(_RUNTIME_CONFIG_SEED)} 个运行时配置项")

            return True

    except Exception as e:
        print(f"   ⚠️  迁移警告: {e}")
        # 不阻止服务启动
        return False


if __name__ == "__main__":
    success1 = check_and_add_unique_constraint()
    success2 = fix_duplicate_count()
    success3 = add_beyond_window_field()
    success4 = add_last_notified_at_field()
    success5 = add_forward_rules_table()
    success6 = add_deep_analyses_table()
    success7 = add_polling_fields()
    success8 = add_archive_and_indexes()
    success9 = add_failed_forwards_table()
    success10 = add_system_configs_table()
    sys.exit(
        0
        if (
            success1
            and success2
            and success3
            and success4
            and success5
            and success6
            and success7
            and success8
            and success9
            and success10
        )
        else 1
    )
