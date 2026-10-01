-- memories 表查询索引（幂等，可重复执行）
CREATE INDEX IF NOT EXISTS idx_memories_user_active
    ON memories (user_id, is_active);

CREATE INDEX IF NOT EXISTS idx_memories_user_type
    ON memories (user_id, memory_type);

CREATE INDEX IF NOT EXISTS idx_memories_conversation
    ON memories (conversation_id);

-- 去重锚点：已由 UNIQUE(user_id, memory_type, content_hash) 原子保证，
-- 不再需要 (user_id + memory_type + content) 的查询前缀索引。
-- 详见 _migrate_memories_content_hash.sql

-- 验证索引
SELECT indexname, tablename
FROM pg_indexes
WHERE tablename = 'memories';
