-- ============================================================
-- User Plans Table（P3 计费 / 配额）
-- Target: user_plans
-- ============================================================
-- 为什么单独一张表而不是只在 config 里配一个全局默认值：
--   计费从第一天起就必然有「某个用户要例外」的需求（内测账号、付费用户、
--   被降级的账号）。全局 config 只能表达「所有人一样」，一旦出现第一个
--   例外就得改代码重新发版。所以默认套餐放 config，per-user 覆盖放这张表。
--
-- 为什么配额是「每天」而不是「每月」：
--   日配额能挡住失控的脚本（一夜之间把额度烧光）；
--   月用量由 Redis 的 m 窗口单独统计，只用于展示与对账，不做拦截——
--   按月硬拦会让「月初用超了就得等到下个月」这种体验问题。
-- ============================================================

CREATE TABLE IF NOT EXISTS "public"."user_plans" (
    "id"                BIGSERIAL PRIMARY KEY,
    "user_uuid"         UUID NOT NULL UNIQUE,
    "plan_code"         VARCHAR(32) NOT NULL DEFAULT 'free',
    -- 每日额度；0 = 不限（用于内测 / 管理员账号）
    "chat_per_day"      INT NOT NULL DEFAULT 200,
    "search_per_day"    INT NOT NULL DEFAULT 50,
    "task_per_day"      INT NOT NULL DEFAULT 10,
    "note"              TEXT,
    "created_at"        TIMESTAMPTZ(6) NOT NULL DEFAULT NOW(),
    "updated_at"        TIMESTAMPTZ(6) NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS "idx_user_plans_plan_code"
    ON "public"."user_plans" ("plan_code");

-- 自动更新 updated_at 触发器
CREATE OR REPLACE FUNCTION "public"."set_user_plans_updated_at"()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS "trg_user_plans_updated_at" ON "public"."user_plans";
CREATE TRIGGER "trg_user_plans_updated_at"
    BEFORE UPDATE ON "public"."user_plans"
    FOR EACH ROW
    EXECUTE FUNCTION "public"."set_user_plans_updated_at"();

-- 说明：没有记录 = 用 config 里的默认套餐（QUOTA_*），
-- 所以这张表平时可以是空的，不需要给每个新用户插一行。
