/*
 Navicat Premium Dump SQL

 Source Server         : localhost_5432
 Source Server Type    : PostgreSQL
 Source Server Version : 160015 (160015)
 Source Host           : localhost:5432
 Source Catalog        : agent_system
 Source Schema         : public

 Target Server Type    : PostgreSQL
 Target Server Version : 160015 (160015)
 File Encoding         : 65001

 Date: 23/09/2026 17:20:28
*/


-- ----------------------------
-- Sequence structure for conversations_id_seq
-- ----------------------------
DROP SEQUENCE IF EXISTS "public"."conversations_id_seq";
CREATE SEQUENCE "public"."conversations_id_seq"
INCREMENT 1
MINVALUE  1
MAXVALUE 9223372036854775807
START 1
CACHE 1;

-- ----------------------------
-- Sequence structure for memories_id_seq
-- ----------------------------
DROP SEQUENCE IF EXISTS "public"."memories_id_seq";
CREATE SEQUENCE "public"."memories_id_seq"
INCREMENT 1
MINVALUE  1
MAXVALUE 9223372036854775807
START 1
CACHE 1;

-- ----------------------------
-- Sequence structure for messages_id_seq
-- ----------------------------
DROP SEQUENCE IF EXISTS "public"."messages_id_seq";
CREATE SEQUENCE "public"."messages_id_seq"
INCREMENT 1
MINVALUE  1
MAXVALUE 9223372036854775807
START 1
CACHE 1;

-- ----------------------------
-- Sequence structure for users_id_seq
-- ----------------------------
DROP SEQUENCE IF EXISTS "public"."users_id_seq";
CREATE SEQUENCE "public"."users_id_seq"
INCREMENT 1
MINVALUE  1
MAXVALUE 9223372036854775807
START 1
CACHE 1;

-- ----------------------------
-- Table structure for conversations
-- ----------------------------
DROP TABLE IF EXISTS "public"."conversations";
CREATE TABLE "public"."conversations" (
  "id" int8 NOT NULL DEFAULT nextval('conversations_id_seq'::regclass),
  "conversation_id" uuid NOT NULL,
  "user_id" int8 NOT NULL,
  "title" varchar(255) COLLATE "pg_catalog"."default",
  "status" int2 NOT NULL DEFAULT 1,
  "message_count" int4 NOT NULL DEFAULT 0,
  "last_message_at" timestamptz(6),
  "created_at" timestamptz(6) NOT NULL DEFAULT now(),
  "updated_at" timestamptz(6) NOT NULL DEFAULT now()
)
;

-- ----------------------------
-- Records of conversations
-- ----------------------------

-- ----------------------------
-- Table structure for memories
-- ----------------------------
DROP TABLE IF EXISTS "public"."memories";
CREATE TABLE "public"."memories" (
  "id" int8 NOT NULL DEFAULT nextval('memories_id_seq'::regclass),
  "memory_id" uuid NOT NULL,
  "user_id" int8 NOT NULL,
  "conversation_id" uuid,
  "memory_type" varchar(50) COLLATE "pg_catalog"."default" NOT NULL,
  "slot" varchar(50) COLLATE "pg_catalog"."default",
  "content" text COLLATE "pg_catalog"."default" NOT NULL,
  "content_hash" varchar(64) COLLATE "pg_catalog"."default" NOT NULL,
  "importance" float8 DEFAULT 0.5,
  "confidence" float8 DEFAULT 0.8,
  "access_count" int4 NOT NULL DEFAULT 0,
  "last_accessed_at" timestamptz(6),
  "created_at" timestamptz(6) NOT NULL DEFAULT now(),
  "updated_at" timestamptz(6) NOT NULL DEFAULT now(),
  "is_active" bool NOT NULL DEFAULT true,
  "metadata" jsonb,
  "sync_status" varchar(20) COLLATE "pg_catalog"."default" NOT NULL DEFAULT 'pending',
  "sync_retry_count" int4 NOT NULL DEFAULT 0,
  "sync_error" text COLLATE "pg_catalog"."default",
  "synced_at" timestamptz(6),
  "retry_after" timestamptz(6),
  "source_message_ids" jsonb NOT NULL DEFAULT '[]'::jsonb,
  "extract_model" varchar(100) COLLATE "pg_catalog"."default",
  "prompt_version" varchar(20) COLLATE "pg_catalog"."default",
  "embedding_model" varchar(100) COLLATE "pg_catalog"."default",
  "merged_into" uuid,
  "deleted_at" timestamptz(6)
)
;

-- ----------------------------
-- Records of memories
-- ----------------------------

-- ----------------------------
-- Table structure for messages
-- ----------------------------
DROP TABLE IF EXISTS "public"."messages";
CREATE TABLE "public"."messages" (
  "id" int8 NOT NULL DEFAULT nextval('messages_id_seq'::regclass),
  "message_id" uuid NOT NULL,
  "conversation_id" uuid NOT NULL,
  "user_id" int8 NOT NULL,
  "role" varchar(20) COLLATE "pg_catalog"."default" NOT NULL,
  "content" text COLLATE "pg_catalog"."default" NOT NULL,
  "sequence_id" int8 NOT NULL,
  "token_count" int4,
  "created_at" timestamptz(6) NOT NULL DEFAULT now(),
  "metadata" jsonb,
  "memory_status" int2 NOT NULL DEFAULT 0
)
;

-- ----------------------------
-- Records of messages
-- ----------------------------

-- ----------------------------
-- Table structure for users
-- ----------------------------
DROP TABLE IF EXISTS "public"."users";
CREATE TABLE "public"."users" (
  "id" int8 NOT NULL DEFAULT nextval('users_id_seq'::regclass),
  "user_uuid" uuid NOT NULL,
  "created_at" timestamptz(6) NOT NULL DEFAULT now(),
  "updated_at" timestamptz(6) NOT NULL DEFAULT now()
)
;

-- ----------------------------
-- Records of users
-- ----------------------------

-- ----------------------------
-- Alter sequences owned by
-- ----------------------------
ALTER SEQUENCE "public"."conversations_id_seq"
OWNED BY "public"."conversations"."id";
SELECT setval('"public"."conversations_id_seq"', 1, false);

-- ----------------------------
-- Alter sequences owned by
-- ----------------------------
ALTER SEQUENCE "public"."memories_id_seq"
OWNED BY "public"."memories"."id";
SELECT setval('"public"."memories_id_seq"', 1, false);

-- ----------------------------
-- Alter sequences owned by
-- ----------------------------
ALTER SEQUENCE "public"."messages_id_seq"
OWNED BY "public"."messages"."id";
SELECT setval('"public"."messages_id_seq"', 1, false);

-- ----------------------------
-- Alter sequences owned by
-- ----------------------------
ALTER SEQUENCE "public"."users_id_seq"
OWNED BY "public"."users"."id";
SELECT setval('"public"."users_id_seq"', 1, false);

-- ----------------------------
-- Uniques structure for table conversations
-- ----------------------------
ALTER TABLE "public"."conversations" ADD CONSTRAINT "conversations_conversation_id_key" UNIQUE ("conversation_id");

-- ----------------------------
-- Primary Key structure for table conversations
-- ----------------------------
ALTER TABLE "public"."conversations" ADD CONSTRAINT "conversations_pkey" PRIMARY KEY ("id");

-- ----------------------------
-- Uniques structure for table memories
-- ----------------------------
ALTER TABLE "public"."memories" ADD CONSTRAINT "memories_memory_id_key" UNIQUE ("memory_id");
ALTER TABLE "public"."memories" ADD CONSTRAINT "memories_user_type_content_hash_key" UNIQUE ("user_id", "memory_type", "content_hash");

-- 属性槽位索引（同槽位收敛 / 写时合并参考）
CREATE INDEX IF NOT EXISTS "memories_user_slot_idx" ON "public"."memories" ("user_id", "slot");

-- ----------------------------
-- Primary Key structure for table memories
-- ----------------------------
ALTER TABLE "public"."memories" ADD CONSTRAINT "memories_pkey" PRIMARY KEY ("id");

-- ----------------------------
-- Uniques structure for table messages
-- ----------------------------
ALTER TABLE "public"."messages" ADD CONSTRAINT "messages_message_id_key" UNIQUE ("message_id");

-- ----------------------------
-- Primary Key structure for table messages
-- ----------------------------
ALTER TABLE "public"."messages" ADD CONSTRAINT "messages_pkey" PRIMARY KEY ("id");

-- ----------------------------
-- Uniques structure for table users
-- ----------------------------
ALTER TABLE "public"."users" ADD CONSTRAINT "users_user_uuid_key" UNIQUE ("user_uuid");

-- ----------------------------
-- Primary Key structure for table users
-- ----------------------------
ALTER TABLE "public"."users" ADD CONSTRAINT "users_pkey" PRIMARY KEY ("id");

-- ----------------------------
-- Foreign Keys structure for table conversations
-- ----------------------------
ALTER TABLE "public"."conversations" ADD CONSTRAINT "fk_conversation_user" FOREIGN KEY ("user_id") REFERENCES "public"."users" ("id") ON DELETE NO ACTION ON UPDATE NO ACTION;
