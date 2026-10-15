/*
 V012 —— attachments 表（P2 附件 / 多模态）

 背景：
   现在 messages 只有 content 文本，用户没法把图片、PDF、CSV 之类的文件
   喂给模型。《现状和不足.md》把它列在「可扩充：附件（图/PDF）→ 多模态」。

 设计要点：
   1. 与 messages / memories 同约定：业务主键用 uuid（attachment_id），
      物理主键用 int8 + 序列，时间列 timestamptz(6) DEFAULT now()。
   2. **不存文件内容到数据库**。文件落本地磁盘（ATTACHMENT_DIR），
      表里只存 storage_path + 抽取出来的文本。理由：
        - Postgres 里塞 10MB blob 会拖垮备份与查询；
        - 文本抽取只需要做一次，落库后可反复用于上下文注入。
   3. message_id 允许为空：上传发生在发消息**之前**（用户先选文件再输入文字），
      真正发出去时才回填绑定。未被任何消息引用的附件由清理任务回收。
   4. conversation_id 允许为空：会话可能尚未创建。

 本迁移是纯建表，不影响任何已有表。
*/

-- ----------------------------
-- Sequence structure for attachments_id_seq
-- ----------------------------
DROP SEQUENCE IF EXISTS "public"."attachments_id_seq";
CREATE SEQUENCE "public"."attachments_id_seq"
INCREMENT 1
MINVALUE  1
MAXVALUE 9223372036854775807
START 1
CACHE 1;

-- ----------------------------
-- Table structure for attachments
-- ----------------------------
DROP TABLE IF EXISTS "public"."attachments";
CREATE TABLE "public"."attachments" (
  "id" int8 NOT NULL DEFAULT nextval('attachments_id_seq'::regclass),
  "attachment_id" uuid NOT NULL,
  "user_id" int8 NOT NULL,
  -- 上传时会话可能还没建（新会话第一条就带附件），因此允许为空
  "conversation_id" uuid,
  -- 真正随消息发出后才回填；空 = 已上传但未被引用（可被清理任务回收）
  "message_id" uuid,
  "name" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "mime" varchar(120) COLLATE "pg_catalog"."default" NOT NULL DEFAULT 'application/octet-stream',
  "size_bytes" int8 NOT NULL DEFAULT 0,
  -- 相对 ATTACHMENT_DIR 的存储路径；为空表示纯元数据（未落盘）
  "storage_path" text COLLATE "pg_catalog"."default",
  -- 抽取出的纯文本（txt/md/csv/pdf…）。图片为空（走 base64 多模态，不抽文本）
  "extracted_text" text COLLATE "pg_catalog"."default",
  "created_at" timestamptz(6) NOT NULL DEFAULT now()
)
;

-- ----------------------------
-- Uniques / Primary Key
-- ----------------------------
ALTER TABLE "public"."attachments" ADD CONSTRAINT "attachments_attachment_id_key" UNIQUE ("attachment_id");
ALTER TABLE "public"."attachments" ADD CONSTRAINT "attachments_pkey" PRIMARY KEY ("id");

-- ----------------------------
-- Indexes
-- ----------------------------
-- 「我的最近附件」+ 孤儿附件清理扫描
CREATE INDEX IF NOT EXISTS "attachments_user_created_idx"
  ON "public"."attachments" ("user_id", "created_at" DESC);
-- 按会话查附件（会话详情页 / 会话删除时级联清理）
CREATE INDEX IF NOT EXISTS "attachments_conversation_id_idx"
  ON "public"."attachments" ("conversation_id");
-- 清理未被引用附件时按 message_id IS NULL 扫
CREATE INDEX IF NOT EXISTS "attachments_message_id_idx"
  ON "public"."attachments" ("message_id");
