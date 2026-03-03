#!/bin/bash
# 立即修复 duplicate_count 的快速脚本
# 可以在远程服务器上直接执行

echo "开始修复 duplicate_count..."

# 必须使用环境变量中的数据库配置
if [ -z "$DATABASE_URL" ]; then
    echo "错误：未设置 DATABASE_URL 环境变量"
    exit 1
fi
DB_URL="$DATABASE_URL"

# 执行 SQL 修复
psql "$DB_URL" <<'EOF'
-- 修复重复告警的 duplicate_count
DO $$
DECLARE
    fixed_count INTEGER;
BEGIN
    -- 执行更新
    UPDATE webhook_events AS duplicate_events
    SET duplicate_count = original_events.duplicate_count
    FROM webhook_events AS original_events
    WHERE duplicate_events.is_duplicate = 1
      AND duplicate_events.duplicate_of = original_events.id
      AND duplicate_events.duplicate_count != original_events.duplicate_count;

    -- 获取受影响的行数
    GET DIAGNOSTICS fixed_count = ROW_COUNT;

    RAISE NOTICE '✅ 成功修复 % 条重复告警记录', fixed_count;
END $$;

-- 验证修复结果（显示前10条）
SELECT
    d.id AS duplicate_id,
    d.duplicate_of AS original_id,
    d.duplicate_count AS count_value,
    o.duplicate_count AS original_count,
    d.timestamp
FROM webhook_events d
JOIN webhook_events o ON d.duplicate_of = o.id
WHERE d.is_duplicate = 1
  AND d.duplicate_count = o.duplicate_count
ORDER BY d.id DESC
LIMIT 10;
EOF

echo ""
echo "修复完成！"
