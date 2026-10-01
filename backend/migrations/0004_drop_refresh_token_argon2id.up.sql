-- ============================================================
-- Name: 0004_drop_refresh_token_argon2id.sql
-- Target: refresh_tokens
-- Version: 1.0
-- Date: 2026-09-26
-- Author: AI
-- Change:
--   1. 删除 lookup_key 列（不再需要 SHA256 单独存，token_hash 本身就是 SHA256）
--   2. 删除 lookup_key 上的 UNIQUE 索引
--   3. 清空旧的 argon2id 格式 token_hash（让旧 Refresh Token 自然失效）
--   4. token_hash 继续承担 UNIQUE 约束（已在 0002 migration 定义）
-- Reason:
--   argon2id 是为人类密码设计的慢速哈希（对抗暴力破解），
--   但 Refresh Token 本身是 crypto/rand 生成的 128-bit 随机串（preimage 完全不可能），
--   SHA256 已完全足够。argon2id 在此场景只有性能开销 + 因随机 salt 不能反查的架构陷阱。
-- Impact: 已登录用户的 Refresh Token 将全部失效（他们需要重新登录）。
--         Access Token（JWT）不受影响，当前有效 token 仍可正常使用直至过期。
-- Rollback: 无法简单回滚（argon2id 格式不可逆）。如需临时回滚，重跑旧版本代码 + 恢复 lookup_key 列：
--   ALTER TABLE refresh_tokens ADD COLUMN lookup_key VARCHAR(64) NOT NULL DEFAULT '';
--   CREATE UNIQUE INDEX idx_refresh_tokens_lookup_key ON refresh_tokens (lookup_key);
-- ============================================================

-- 步骤 1：删 lookup_key 列（连同其 UNIQUE 索引 idx_refresh_tokens_lookup_key）
DROP INDEX IF EXISTS "idx_refresh_tokens_lookup_key";
ALTER TABLE "public"."refresh_tokens" DROP COLUMN IF EXISTS "lookup_key";

-- 步骤 2：清空所有 Refresh Token（让旧 argon2id 格式 token 全部失效，用户重新登录即可获得新 token）
-- 这比"保留 argon2id 格式的旧记录 + 新记录写 SHA256"更干净：
--   - 不会有 token_hash 列里混杂两种格式
--   - 不会有旧 token 在 Refresh 时因 hash 不匹配而报 INVALID_TOKEN
UPDATE "public"."refresh_tokens"
   SET revoked_at = NOW()
 WHERE revoked_at IS NULL;
