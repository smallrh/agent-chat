-- ============================================================
-- Refresh Tokens Table
-- 存储 Refresh Token 的哈希（不是明文），支持 Rotation + Replay 检测
-- ============================================================

CREATE TABLE IF NOT EXISTS "public"."refresh_tokens" (
    "id"                BIGSERIAL PRIMARY KEY,
    "auth_user_id"      BIGINT NOT NULL REFERENCES "public"."auth_users"("id") ON DELETE CASCADE,

    -- Argon2id 哈希后的 Refresh Token 标识
    "token_hash"        VARCHAR(256) NOT NULL UNIQUE,

    -- 轮换链：本次刷新是从哪个 token 来的（用于 Replay 检测）
    "parent_token_id"   BIGINT REFERENCES "public"."refresh_tokens"("id") ON DELETE SET NULL,

    "device_id"         VARCHAR(128),
    "user_agent"        VARCHAR(512),

    "expires_at"        TIMESTAMPTZ(6) NOT NULL,
    "revoked_at"        TIMESTAMPTZ(6),   -- 非空 = 已撤销/已轮换失效

    "created_at"        TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS "idx_refresh_tokens_user_id"
    ON "public"."refresh_tokens" ("auth_user_id");

CREATE INDEX IF NOT EXISTS "idx_refresh_tokens_token_hash"
    ON "public"."refresh_tokens" ("token_hash");

CREATE INDEX IF NOT EXISTS "idx_refresh_tokens_expires_at"
    ON "public"."refresh_tokens" ("expires_at");

CREATE INDEX IF NOT EXISTS "idx_refresh_tokens_revoked_at"
    ON "public"."refresh_tokens" ("revoked_at");
