-- 补齐 memories.slot 列
--
-- 背景：slot 是演进期新增的列（用于"同属性槽位只保留最新值"的冲突收敛，
-- 见 ADR-004 / ADR-005）。public.sql 的最新建表 DDL 里已经有 slot，
-- 但**已有数据库不会自动补列** —— 六个 _migrate_*.sql 手工脚本里
-- 也没有一个加过它。
--
-- 后果（很隐蔽）：
--   - MemoryRepository.save() 的 INSERT 引用了 slot 列 → 每条记忆都写不进去
--   - ProfileService.rebuild_from_memories() 的 SELECT 引用了 slot 列 → 每次都崩，
--     降级返回 {} → profile 块永远为空 → 新会话"记不住用户"
--   两段代码各自 try/except 吞异常，只在 DEBUG 日志里留痕，所以线上看着"没报错"。
--
-- 用法（已有库执行一次即可，全新环境不需要 —— V001 建表已含 slot）：
--     psql -h <host> -U <user> -d <db> -f migrations/V010__memories_slot.sql
--
-- 全部语句幂等，可重复执行。

ALTER TABLE "public"."memories"
    ADD COLUMN IF NOT EXISTS "slot" varchar(50) COLLATE "pg_catalog"."default";

-- 老数据回填：缺 slot 时用 memory_type 兜底，
-- 与 ProfileService._profile_key 的回退语义保持一致。
UPDATE "public"."memories"
   SET "slot" = "memory_type"
 WHERE "slot" IS NULL OR "slot" = '';

CREATE INDEX IF NOT EXISTS "memories_user_slot_idx"
    ON "public"."memories" ("user_id", "slot");
