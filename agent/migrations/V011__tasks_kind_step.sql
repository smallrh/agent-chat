/*
 V011 —— tasks 表补「任务类型」与「细粒度进度」

 背景（P2 真任务队列）：
   V009 建的 tasks 表只有 status/progress(0-100) 两列状态信息，但实际使用时
   暴露出三个问题：
     1) API 接受了 kind 参数（demo / research …），但**没有列存它** ——
        任务落库后类型就丢了，前端只能看到「一个在跑的东西」，无法按类型
        渲染图标/文案，重启恢复时也无法知道该用什么 handler。
     2) progress 只有一个百分比，前端进度条下面没东西可显示。真实长任务
        需要「当前在做什么」（正在检索资料 3/5）这种粒度的反馈。
     3) 只有 progress 没有 done/total，前端算不出「第几步」，重启恢复、
        重跑（retry）也都缺依据。

 本迁移是**纯增量**（只加列和索引），不影响已有数据行。
 新增列全部带默认值，已有行自动补 'demo' / 空 step / 0。
*/

-- 任务类型：与 TaskService 注册的 handler kind 一一对应
--   demo     —— 内置演示 handler（无外部依赖）
--   research —— 真实调研 worker（拆子查询 → 联网检索 → LLM 汇总）
ALTER TABLE "public"."tasks"
  ADD COLUMN IF NOT EXISTS "kind" varchar(32) COLLATE "pg_catalog"."default" NOT NULL DEFAULT 'demo';

-- 当前步骤的人类可读描述（如「检索资料：广州天河酒店」）
ALTER TABLE "public"."tasks"
  ADD COLUMN IF NOT EXISTS "step" varchar(255) COLLATE "pg_catalog"."default";

-- 步骤计数：steps_done / steps_total，前端可据此渲染「3/5」
ALTER TABLE "public"."tasks"
  ADD COLUMN IF NOT EXISTS "steps_done" int2 NOT NULL DEFAULT 0;
ALTER TABLE "public"."tasks"
  ADD COLUMN IF NOT EXISTS "steps_total" int2 NOT NULL DEFAULT 0;

-- 按类型筛（例如「我所有调研任务」）
CREATE INDEX IF NOT EXISTS "tasks_user_kind_idx"
  ON "public"."tasks" ("user_id", "kind");

-- 任务中心页按用户全量查看（不带 conversation_id 的查询）
-- 已有 tasks_user_status_idx(user_id, status) 覆盖带状态的筛选，
-- 这里补 (user_id, created_at DESC) 支持「最近任务」全量列表。
CREATE INDEX IF NOT EXISTS "tasks_user_created_idx"
  ON "public"."tasks" ("user_id", "created_at" DESC);
