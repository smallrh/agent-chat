-- =====================================================================
-- memories 近似合并：merged_into + is_active 索引
--
-- 设计：
--   merged_into  uuid NULL  — 指向"被合并到的那条"（保留的记忆）
--                              被合并那条 is_active=false, merged_into=保留那条 memory_id
--   is_active    bool       — 已存在列，直接用
--
-- 合并策略：
--   同 user + 同 memory_type + cosine_similarity ≥ 阈值(0.85)
--   → 保留已落库那条 (is_active=true), 新的被标记 merged_into 指向它
--   → 保留的那条向量正常在 Qdrant
--   → 被合并的那条 Qdrant 向量被自然过滤（payload.is_active=false）
-- =====================================================================

-- 1) 加列
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS merged_into uuid;

-- 2) 查询索引：is_active=false 的只被合并了的"僵尸行"，快速过滤
CREATE INDEX IF NOT EXISTS idx_memories_active_type
    ON memories (is_active, memory_type);

CREATE INDEX IF NOT EXISTS idx_memories_merged_into
    ON memories (merged_into);

-- 验证
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'memories' AND column_name = 'merged_into';
