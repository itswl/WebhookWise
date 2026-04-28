-- 1. 创建归档表
CREATE TABLE IF NOT EXISTS archived_webhook_events (
    id INTEGER PRIMARY KEY,
    source VARCHAR(100) NOT NULL,
    client_ip VARCHAR(50),
    timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    raw_payload TEXT,
    headers JSON,
    parsed_data JSON,
    alert_hash VARCHAR(64),
    ai_analysis JSON,
    importance VARCHAR(20),
    forward_status VARCHAR(20),
    is_duplicate INTEGER,
    duplicate_of INTEGER,
    duplicate_count INTEGER,
    beyond_window INTEGER,
    last_notified_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE,
    updated_at TIMESTAMP WITHOUT TIME ZONE,
    archived_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_archived_timestamp ON archived_webhook_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_archived_hash_timestamp ON archived_webhook_events(alert_hash, timestamp);

-- 2. 优化活跃表索引
-- 复合索引：优化去重查询 (alert_hash + timestamp)
CREATE INDEX IF NOT EXISTS idx_webhook_hash_timestamp
ON webhook_events(alert_hash, timestamp);

-- 复合索引：优化降噪上下文扫描 (importance + timestamp)
CREATE INDEX IF NOT EXISTS idx_webhook_importance_timestamp
ON webhook_events(importance, timestamp);

-- 复合索引：优化重复查找 (alert_hash + is_duplicate + timestamp)
CREATE INDEX IF NOT EXISTS idx_webhook_duplicate_lookup
ON webhook_events(alert_hash, is_duplicate, timestamp);

-- 3. 分析表结构
ANALYZE webhook_events;
