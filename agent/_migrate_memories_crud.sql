-- =====================================================================
-- memories CRUD：软删 + 版本化 + 查询
--
-- 设计：
--   deleted_at  timestamptz NULL — 软删时间戳
--     is_active=false + deleted_at=now() = "已删除"
--     is_active=false + deleted_at=NULL + merged_into!=NULL = "被合并/被替代"
--
--   merged_into 列复用语义：
--     近似合并 → merged_into=保留那条
--     版本化修改 → 老行 merged_into=新行_id（新行 is_active=true）
--     软删 → merged_into=NULL, is_active=false, deleted_at=now()
-- =====================================================================

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- 查询索引：按 user + active 过滤（list 主要路径）
CREATE INDEX IF NOT EXISTS idx_memories_user_active
    ON memories (user_id, is_active)
    WHERE is_active = true;

-- 单条查询索引
CREATE INDEX IF NOT EXISTS idx_memories_memory_id_active
    ON memories (memory_id, is_active);
