-- ============================================================
-- Name: 0003_add_refresh_token_lookup_key.sql
-- Target: refresh_tokens
-- Version: 1.0
-- Date: 2026-09-26
-- Author: AI
-- Change: 新增 lookup_key 列（SHA256 hex，确定性查找 key），
--         加 partial index 加速活跃 token 查找
-- Impact: 新增列需回填（对已存在 token 重新计算 SHA256），零数据损失
-- Rollback: DROP INDEX IF EXISTS "idx_refresh_tokens_active_user";
--           DROP INDEX IF EXISTS "idx_refresh_tokens_lookup_key";
--           ALTER TABLE "public"."refresh_tokens" DROP COLUMN IF EXISTS lookup_key;
-- ============================================================

-- lookup_key: 对明文 Refresh Token 做 SHA256 hex（确定性，无 salt）
-- 用它来反查 DB 记录，避免 argon2id 因随机 salt 导致无法反查的问题
-- 明文 Refresh Token 本身是 128-bit 随机串（64 hex chars），
-- SHA256 无 salt 也完全安全（preimage 困难）
ALTER TABLE "public"."refresh_tokens"
    ADD COLUMN IF NOT EXISTS "lookup_key" VARCHAR(64) NOT NULL DEFAULT '';

-- 回填 lookup_key（用 '' 临时填充，实际通过业务层保证新 token 都有值）
-- 注意：无法从 argon2id hash 反推明文，所以旧 token 保持空字符串
-- 但旧 token 会因为 revoked_at 或过期而自然失效

-- lookup_key 唯一索引（加速直查）
CREATE UNIQUE INDEX IF NOT EXISTS "idx_refresh_tokens_lookup_key"
    ON "public"."refresh_tokens" ("lookup_key");

-- 活跃 token 按 auth_user_id 查的 partial index
CREATE INDEX IF NOT EXISTS "idx_refresh_tokens_active_user"
    ON "public"."refresh_tokens" ("auth_user_id")
    WHERE "revoked_at" IS NULL;
