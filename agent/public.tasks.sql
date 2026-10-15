/*
 tasks 表 —— P2 多任务（任务状态 / 进度 / 后台写回）

 约定与现有表保持一致：
   - 主键用 int8 + 序列（和 conversations/messages/memories/users 一致）
   - task_id 用 uuid 作为对外稳定标识（和 message_id/conversation_id 一致）
   - timestamps 用 timestamptz(6) DEFAULT now()
   - 复用 conversations(conversation_id) 作为 FK（uuid 列）

 该文件是独立迁移，可单独执行，不影响现有表。
 执行方式（示例）：
   psql "$DATABASE_URL" -f public.tasks.sql
*/

-- ----------------------------
-- Sequence structure for tasks_id_seq
-- ----------------------------
DROP SEQUENCE IF EXISTS "public"."tasks_id_seq";
CREATE SEQUENCE "public"."tasks_id_seq"
INCREMENT 1
MINVALUE  1
MAXVALUE 9223372036854775807
START 1
CACHE 1;

-- ----------------------------
-- Table structure for tasks
-- ----------------------------
DROP TABLE IF EXISTS "public"."tasks";
CREATE TABLE "public"."tasks" (
  "id" int8 NOT NULL DEFAULT nextval('tasks_id_seq'::regclass),
  "task_id" uuid NOT NULL,
  "conversation_id" uuid NOT NULL,
  "user_id" int8 NOT NULL,
  "title" varchar(255) COLLATE "pg_catalog"."default",
  "goal" text COLLATE "pg_catalog"."default",
  -- 状态机：pending -> running -> succeeded | failed | cancelled
  "status" varchar(20) COLLATE "pg_catalog"."default" NOT NULL DEFAULT 'pending',
  -- 进度百分比 0..100
  "progress" int2 NOT NULL DEFAULT 0,
  -- 任务结果（任意 JSON：摘要 / 产物 / 子步骤）
  "result" jsonb,
  -- 失败原因（仅 status=failed 时有值）
  "error" text COLLATE "pg_catalog"."default",
  "created_at" timestamptz(6) NOT NULL DEFAULT now(),
  "updated_at" timestamptz(6) NOT NULL DEFAULT now()
)
;

-- ----------------------------
-- Uniques / Primary Key
-- ----------------------------
ALTER TABLE "public"."tasks" ADD CONSTRAINT "tasks_task_id_key" UNIQUE ("task_id");
ALTER TABLE "public"."tasks" ADD CONSTRAINT "tasks_pkey" PRIMARY KEY ("id");

-- ----------------------------
-- Indexes
-- ----------------------------
-- 按会话列出任务（前端「本会话的任务」最常见查询）
CREATE INDEX IF NOT EXISTS "tasks_conversation_id_idx"
  ON "public"."tasks" ("conversation_id");
-- 按用户 + 状态筛（例如「我所有进行中的任务」）
CREATE INDEX IF NOT EXISTS "tasks_user_status_idx"
  ON "public"."tasks" ("user_id", "status");

-- ----------------------------
-- Foreign Keys
-- ----------------------------
ALTER TABLE "public"."tasks" ADD CONSTRAINT "fk_task_conversation"
  FOREIGN KEY ("conversation_id")
  REFERENCES "public"."conversations" ("conversation_id")
  ON DELETE CASCADE
  ON UPDATE NO ACTION;
