-- 添加唯一约束防止重复告警
-- 此脚本为告警去重添加数据库层面的强制约束

-- 1. 先检查是否存在 alert_hash 为空的原始告警
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM webhook_events
        WHERE alert_hash IS NULL AND is_duplicate = 0
    ) THEN
        RAISE NOTICE '检测到 alert_hash 为空的原始告警，将设置默认值';

        -- 为空值设置唯一的哈希（使用 ID + 时间戳）
        UPDATE webhook_events
        SET alert_hash = md5(id::text || timestamp::text)
        WHERE alert_hash IS NULL AND is_duplicate = 0;
    END IF;
END $$;

-- 2. 创建部分唯一索引（只对原始告警生效）
-- 这样可以允许多个重复告警（is_duplicate=1）指向同一个原始告警
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original
ON webhook_events(alert_hash)
WHERE is_duplicate = 0;

-- 3. 添加注释
COMMENT ON INDEX idx_unique_alert_hash_original IS
'确保相同 alert_hash 只有一个原始告警（is_duplicate=0），防止并发插入导致的重复';

-- 4. 验证约束
DO $$
DECLARE
    duplicate_count INTEGER;
BEGIN
    -- 检查是否存在重复的原始告警
    SELECT COUNT(*) INTO duplicate_count
    FROM (
        SELECT alert_hash, COUNT(*) as cnt
        FROM webhook_events
        WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
        GROUP BY alert_hash
        HAVING COUNT(*) > 1
    ) AS duplicates;

    IF duplicate_count > 0 THEN
        RAISE WARNING '发现 % 组重复的原始告警，需要手动处理', duplicate_count;

        -- 显示重复的告警
        RAISE NOTICE '重复告警详情：';
        FOR rec IN
            SELECT alert_hash, COUNT(*) as cnt, array_agg(id ORDER BY timestamp) as ids
            FROM webhook_events
            WHERE is_duplicate = 0 AND alert_hash IS NOT NULL
            GROUP BY alert_hash
            HAVING COUNT(*) > 1
        LOOP
            RAISE NOTICE 'alert_hash=%, count=%, ids=%', rec.alert_hash, rec.cnt, rec.ids;
        END LOOP;
    ELSE
        RAISE NOTICE '✅ 无重复告警，约束已成功创建';
    END IF;
END $$;
