import json

import redis.asyncio as redis

from app.core.config import settings


class RedisRepository:

    def __init__(self):
        self.client: redis.Redis | None = None

    # =========================
    # Redis 生命周期
    # =========================

    async def start(self):
        self.client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=getattr(settings, "REDIS_PASSWORD", None),
            db=getattr(settings, "REDIS_DB", 0),
            decode_responses=True,
        )

        await self.client.ping()

    async def stop(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    # =========================
    # Key
    # =========================

    @staticmethod
    def message_key(conversation_id: str) -> str:
        return f"{settings.REDIS_KEY_PREFIX}conversation:{conversation_id}:messages"

    @staticmethod
    def summary_key(conversation_id: str) -> str:
        return f"{settings.REDIS_KEY_PREFIX}conversation:{conversation_id}:summary"

    @staticmethod
    def memories_key(user_id: int | str) -> str:
        return f"{settings.REDIS_KEY_PREFIX}user:{user_id}:memories"

    @staticmethod
    def profile_key(user_id: int | str) -> str:
        return f"{settings.REDIS_KEY_PREFIX}user:{user_id}:profile"

    # =========================
    # 最近消息
    # =========================

    async def push_message(self, event: dict):
        """
        保存最近对话消息。

        Redis:
            conversation:{conversation_id}:messages

        只保留最近 20 条。
        """

        conversation_id = event["conversation_id"]

        key = self.message_key(conversation_id)

        message = {
            "message_id": event["message_id"],
            "user_id": event["user_id"],
            "conversation_id": event["conversation_id"],
            "role": event["role"],
            "content": event["content"],
            "sequence_id": event.get("sequence_id"),
            "timestamp": event["timestamp"],
        }

        await self.client.lpush(
            key,
            json.dumps(message, ensure_ascii=False),
        )

        # 只保留最近 20 条
        await self.client.ltrim(
            key,
            0,
            19,
        )

    async def get_recent_messages(self,conversation_id: str,limit: int = 20,) -> list[dict]:

        key = self.message_key(conversation_id)

        items = await self.client.lrange(
            key,
            0,
            limit - 1,
        )

        return [
            json.loads(item)
            for item in reversed(items)
        ]

    # =========================
    # Conversation Summary
    # =========================

    async def set_summary(self,conversation_id: str,summary: str):
        """
        保存对话摘要。

        Redis:
            conversation:{conversation_id}:summary
        """

        key = self.summary_key(conversation_id)

        await self.client.set(
            key,
            summary,
        )

    async def get_summary(self,conversation_id: str) -> str | None:

        key = self.summary_key(conversation_id)

        return await self.client.get(key)

    async def delete_summary(self,conversation_id: str):
        key = self.summary_key(conversation_id)

        await self.client.delete(key)

    # =========================
    # User Memories
    # =========================

    async def push_memory(self,user_id: int | str,memory: dict,limit: int = 20):
        """
        把一条 memory 追加到 Redis 最近记忆缓存 List。

        Redis:
            user:{user_id}:memories → List，最新 limit 条

        注意：去重已由 PG UNIQUE(user_id, memory_type, content_hash) 原子保证，
              这里 Redis 只做"最近 N 条"的纯缓存，不再参与去重判断。
              PG 如果冲突返回 None，上层会 skip 整条链路，Redis 也不会收到写入请求。
        """

        key = self.memories_key(user_id)

        await self.client.lpush(
            key,
            json.dumps(
                memory,
                ensure_ascii=False,
            ),
        )
        await self.client.ltrim(
            key,
            0,
            limit - 1,
        )

    async def get_memories(self,user_id: int | str,limit: int = 20) -> list[dict]:

        key = self.memories_key(user_id)

        items = await self.client.lrange(
            key,
            0,
            limit - 1,
        )

        return [
            json.loads(item)
            for item in reversed(items)
        ]

    async def clear_memories(self,user_id: int | str):
        key = self.memories_key(user_id)

        await self.client.delete(key)

    # =========================
    # User Profile
    # =========================

    async def set_profile(self,user_id: int | str,profile: dict,):
        """
        保存用户画像。

        Redis:
            user:{user_id}:profile
        """

        key = self.profile_key(user_id)

        await self.client.set(
            key,
            json.dumps(
                profile,
                ensure_ascii=False,
            ),
        )

    async def get_profile(self,user_id: int | str) -> dict | None:

        key = self.profile_key(user_id)

        data = await self.client.get(key)

        if not data:
            return None

        return json.loads(data)

    async def delete_profile(self,user_id: int | str):
        key = self.profile_key(user_id)

        await self.client.delete(key)