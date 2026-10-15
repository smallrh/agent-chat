-- ============================================================
-- Rollback: 0005_create_user_plans
-- ============================================================
-- 注意：回滚后所有用户回到 config 里的全局默认套餐（QUOTA_*）。
-- 如果有用户被单独调过额度，那些覆盖值会丢失，需要人工补回。
-- ============================================================

DROP TRIGGER IF EXISTS "trg_user_plans_updated_at" ON "public"."user_plans";
DROP FUNCTION IF EXISTS "public"."set_user_plans_updated_at"();

DROP INDEX IF EXISTS "idx_user_plans_plan_code";

DROP TABLE IF EXISTS "public"."user_plans";
