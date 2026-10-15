-- =====================================================================
-- memories 表 sync 状态列：追踪 embed/Qdrant 失败的孤儿记忆
--
-- 设计：
--   sync_status      pending / indexed     只有两态，简单清晰
--   sync_retry_count int 重试次数，驱动指数退避
--   sync_error       text  最后一次失败原因（截断 500）
--   synced_at        timestamptz  最近一次成功/失败落库时间
--   retry_after      timestamptz  下次允许重试的时间（指数退避）
--
-- backoff 策略：
--   第 1 次失败 → retry_after = now() + 1min
--   第 2 次失败 → retry_after = now() + 2min
--   第 3 次失败 → retry_after = now() + 4min
--   ...  上限 24h
--   （由 reconcile_consumer 里的 Python 代码算，PG 只存字段）
-- =====================================================================

-- 1) 加列（允许 NULL，先回填再改 NOT NULL）
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS sync_status varchar(20) DEFAULT 'pending';

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS sync_retry_count int NOT NULL DEFAULT 0;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS sync_error text;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS synced_at timestamptz;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS retry_after timestamptz;

-- 2) 回填所有现有行为 pending（因为之前没有 sync 机制，Qdrant 里可能没有对应向量）
UPDATE memories
SET sync_status = 'pending'
WHERE sync_status IS NULL;

-- 3) 改 NOT NULL（sync_status）
ALTER TABLE memories
    ALTER COLUMN sync_status SET NOT NULL;

-- 4) 查询索引：reconcile 扫 pending + 到期可重试
CREATE INDEX IF NOT EXISTS idx_memories_sync_pending
    ON memories (sync_status, retry_after);

CREATE INDEX IF NOT EXISTS idx_memories_user_sync
    ON memories (user_id, sync_status);

-- 验证
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'memories'
ORDER BY ordinal_position;
