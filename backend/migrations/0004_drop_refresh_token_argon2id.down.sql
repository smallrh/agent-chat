-- 无法简单回滚：argon2id 是单向哈希，旧 token_hash 里的 argon2id 格式无法逆推回明文，
-- lookup_key 也无法从 argon2id hash 还原。本脚本只恢复表结构，
-- 但 refresh_tokens 中所有记录的 token_hash 已被 0004.up.sql 清空为 '' 或保持 argon2id 格式，
-- 回滚后旧 token 仍不可用（需用户重新登录）。

-- 恢复 lookup_key 列（但无法回填值，用户重新登录后新 token 会自动有值）
ALTER TABLE "public"."refresh_tokens"
    ADD COLUMN IF NOT EXISTS "lookup_key" VARCHAR(64) NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS "idx_refresh_tokens_lookup_key"
    ON "public"."refresh_tokens" ("lookup_key");
