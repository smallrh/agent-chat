DROP INDEX IF EXISTS "idx_refresh_tokens_active_user";
DROP INDEX IF EXISTS "idx_refresh_tokens_lookup_key";
ALTER TABLE "public"."refresh_tokens" DROP COLUMN IF EXISTS "lookup_key";
