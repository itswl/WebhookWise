-- 修复重复告警的 duplicate_count 字段
-- 问题：重复告警记录的 duplicate_count 都是 1，应该继承原始告警的累计次数

-- 更新所有重复告警的 duplicate_count
-- 从原始告警继承当前的累计重复次数
UPDATE webhook_events AS duplicate_events
SET duplicate_count = original_events.duplicate_count
FROM webhook_events AS original_events
WHERE duplicate_events.is_duplicate = 1
  AND duplicate_events.duplicate_of = original_events.id
  AND duplicate_events.duplicate_count != original_events.duplicate_count;

-- 统计修复的记录数
DO $$
DECLARE
    fixed_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO fixed_count
    FROM webhook_events AS duplicate_events
    JOIN webhook_events AS original_events
        ON duplicate_events.duplicate_of = original_events.id
    WHERE duplicate_events.is_duplicate = 1
      AND duplicate_events.duplicate_count = original_events.duplicate_count;

    RAISE NOTICE '已修复 % 条重复告警记录的 duplicate_count 字段', fixed_count;
END $$;
