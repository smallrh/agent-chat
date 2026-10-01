-- =====================================================================
-- memories 溯源补全：记录"哪条消息 / 哪个模型 / 哪个 prompt 版本"产生
--
-- 设计：
--   source_message_ids  jsonb[] — 一条 memory 可能来自多条 user 消息综合判断
--                                  默认 []，process_memory 里取批次内所有 user 消息的 message_id
--   extract_model       varchar(100) — 抽取用的 LLM（settings.OPENAI_MODEL）
--   prompt_version      varchar(20) — 手动 bump（v1 / v2 / ...）
--   embedding_model     varchar(100) — 向量模型（settings.EMBEDDING_MODEL）
--
-- 老数据全部 NULL，查询时 WHERE extract_model IS NOT NULL 区分新旧
-- =====================================================================

-- 1) 加列
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS source_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS extract_model varchar(100);

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS prompt_version varchar(20);

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_model varchar(100);

-- 2) 查询索引：按 prompt_version / extract_model 过滤（运维排查用）
CREATE INDEX IF NOT EXISTS idx_memories_prompt_version
    ON memories (prompt_version);

CREATE INDEX IF NOT EXISTS idx_memories_extract_model
    ON memories (extract_model);

-- 验证
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'memories'
  AND column_name IN ('source_message_ids','extract_model','prompt_version','embedding_model')
ORDER BY ordinal_position;
