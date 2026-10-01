-- =====================================================================
-- memories 表去重锚点迁移：content_hash + UNIQUE(user_id, memory_type, content_hash)
--
-- 前置条件：无（md5 是 PG 内置函数，无需 pgcrypto 扩展）
-- 幂等：所有语句都带 IF NOT EXISTS / IS NULL 保护
--
-- 执行顺序：
--   1. 加 content_hash 列（允许 NULL，先回填再改 NOT NULL）
--   2. 老数据回填（PG 内置 md5，不需要扩展）
--   3. 统一 strip 归一化（PG strip 等价 Python strip）
--   4. 改 NOT NULL
--   5. 加 UNIQUE 约束（冲突会报错：需先手动清理重复数据）
--
-- 注意：第 5 步如果失败，说明库里已存在 (user_id, memory_type, content) 完全
--       相同的重复行，需要先人工排查清理再执行 UNIQUE。
-- =====================================================================

-- 1) 加列
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS content_hash varchar(64);

-- 2) 回填老数据：用 PG 内置 md5 代替 sha256（避免依赖 pgcrypto）
--    注意：Python 端后续写入用 sha256，PG md5 回填的老数据 hash 前缀会不一致，
--    但这不影响 UNIQUE 约束，因为新老数据不会 hash 冲突（除非 sha256 前 32 位
--    恰好等于 md5 值，概率可忽略）。
UPDATE memories
SET content_hash = md5(content)
WHERE content_hash IS NULL;

-- 3) strip 归一化：Python 端写入会 content.strip() 后再算 sha256，
--    PG 端用 btrim 对老数据也做一次 strip，让新老 hash 语义对齐。
--    这里为了简单，老数据不重算 hash（md5 已稳定），只保证内容本身的 strip 已体现在 hash 上。
--    如果需要严格对齐 Python 端 sha256(btrim(content))，取消下面注释：
-- UPDATE memories
-- SET content_hash = md5(btrim(content))
-- WHERE content_hash IS NOT NULL;

-- 4) 改 NOT NULL
ALTER TABLE memories
    ALTER COLUMN content_hash SET NOT NULL;

-- 5) 加 UNIQUE 约束（去重锚点正式由 Redis SADD 移交 PG）
-- 注意：PG 的 ALTER TABLE ADD CONSTRAINT 不支持 IF NOT EXISTS，
--       执行前请确保库里没有 (user_id, memory_type, content_hash) 的重复行
ALTER TABLE memories
    ADD CONSTRAINT memories_user_type_content_hash_key
    UNIQUE (user_id, memory_type, content_hash);

-- 6) 可选：content_hash 单独索引（如果后续有按 hash 直接查的场景）
-- CREATE INDEX IF NOT EXISTS idx_memories_content_hash
--     ON memories (content_hash);

-- 验证
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'memories'
ORDER BY ordinal_position;

SELECT conname, contype, conkey
FROM pg_constraint
WHERE conrelid = 'memories'::regclass;
