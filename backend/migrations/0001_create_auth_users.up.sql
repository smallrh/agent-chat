-- ============================================================
-- Auth Users Table
-- 独立认证表，与 Python Agent 的 users 表并存
-- 通过 auth_user_uuid = users.user_uuid 关联
-- ============================================================

CREATE TABLE IF NOT EXISTS "public"."auth_users" (
    "id"                BIGSERIAL PRIMARY KEY,
    "auth_user_uuid"    UUID NOT NULL UNIQUE,
    "username"          VARCHAR(128) NOT NULL UNIQUE,
    "email"             VARCHAR(256) NOT NULL UNIQUE,
    "password_hash"     VARCHAR(256) NOT NULL,
    "status"            SMALLINT NOT NULL DEFAULT 1,
    -- status: 1=active, 2=disabled

    "created_at"        TIMESTAMPTZ(6) NOT NULL DEFAULT NOW(),
    "updated_at"        TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS "idx_auth_users_status"
    ON "public"."auth_users" ("status");

CREATE INDEX IF NOT EXISTS "idx_auth_users_created_at"
    ON "public"."auth_users" ("created_at" DESC);

-- 自动更新 updated_at 触发器
CREATE OR REPLACE FUNCTION "public"."set_auth_users_updated_at"()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS "trg_auth_users_updated_at" ON "public"."auth_users";
CREATE TRIGGER "trg_auth_users_updated_at"
    BEFORE UPDATE ON "public"."auth_users"
    FOR EACH ROW
    EXECUTE FUNCTION "public"."set_auth_users_updated_at"();
