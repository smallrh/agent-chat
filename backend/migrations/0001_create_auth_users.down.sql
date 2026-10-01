DROP TRIGGER IF EXISTS "trg_auth_users_updated_at" ON "public"."auth_users";
DROP FUNCTION IF EXISTS "public"."set_auth_users_updated_at"();
DROP TABLE IF EXISTS "public"."auth_users";
