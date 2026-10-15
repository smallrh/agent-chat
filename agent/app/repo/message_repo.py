import json
import uuid as _uuid
from datetime import datetime

import asyncpg
from fastapi import HTTPException, status


from app.core.config import settings
from app.kafka.producer import logger
from app.repo.pg import get_pool


class MessageRepository:

    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def start(self):
        """取共享连接池（幂等，已有池直接复用）。见 app/repo/pg.py。"""
        if self.pool is not None:
            return
        self.pool = await get_pool()

    async def stop(self):
        # 共享池不在这里关闭（别的组件还在用），只解引用。
        self.pool = None

    # ----------------------------------------------------------------
    # conversation ownership 校验
    # ----------------------------------------------------------------

    async def ensure_conversation_owner(
        self,
        conversation_id: str,
        user_id: int,
    ) -> None:
        """校验 conversation_id 属于当前 user_id。

        三种情况：
        1. 表里没有这条 conversation（全新会话）→ 直接插一条带正确 user_id
           的 conversations 记录并返回，后续落库的 INSERT ON CONFLICT 会复用。
        2. 表里有且 user_id 匹配 → 返回，允许继续读写。
        3. 表里有但 user_id 不匹配 → 抛 HTTPException(403)，阻断请求。

        这个方法在 ChatService 每次进入 chat()/stream_chat() 最开始调用，
        保证 Redis 读上下文、PG 落库都过 ownership 校验。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        conv_uuid = _uuid.UUID(conversation_id)

        row = await self.pool.fetchrow(
            """
            SELECT user_id
            FROM conversations
            WHERE conversation_id = $1
            """,
            conv_uuid,
        )

        if row is None:
            # 新会话 —— 占坑，绑定 user_id
            try:
                await self.pool.execute(
                    """
                    INSERT INTO conversations (
                        conversation_id,
                        user_id,
                        created_at,
                        last_message_at
                    )
                    VALUES ($1, $2, now(), now())
                    """,
                    conv_uuid,
                    user_id,
                )
                logger.info(
                    "ensure_conversation_owner: created new conversation "
                    "conv=%s user_id=%d",
                    conversation_id, user_id,
                )
            except asyncpg.UniqueViolationError:
                # 并发：另一个请求刚好抢先插了，再查一次
                row2 = await self.pool.fetchrow(
                    "SELECT user_id FROM conversations WHERE conversation_id = $1",
                    conv_uuid,
                )
                if row2 is not None and int(row2["user_id"]) != user_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=(
                            f"Conversation {conversation_id} does not belong "
                            f"to current user."
                        ),
                    )
            return

        # 已存在 —— 比 owner
        owner_id = int(row["user_id"])
        if owner_id != user_id:
            logger.warning(
                "ensure_conversation_owner DENY: conv=%s caller_user_id=%d "
                "owner_user_id=%d",
                conversation_id, user_id, owner_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Conversation {conversation_id} does not belong "
                    f"to current user."
                ),
            )

    @staticmethod
    def _to_uuid(value) -> _uuid.UUID:
        """将字符串转成 uuid.UUID，asyncpg 才能正确写入 uuid 列。"""
        if isinstance(value, _uuid.UUID):
            return value
        return _uuid.UUID(str(value))

    @staticmethod
    def _to_int(value) -> int:
        """将数字字符串转成 int，asyncpg 才能正确写入 int8 列。"""
        if isinstance(value, int):
            return value
        return int(value)

    @staticmethod
    def _to_datetime(value) -> datetime:
        """将 ISO 8601 字符串转成 datetime，asyncpg 才能正确写入 timestamptz 列。"""
        if isinstance(value, datetime):
            return value
        # Python 3.11+ fromisoformat 支持 'Z'，这里兼容一下
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)

    @staticmethod
    def _to_jsonb(value) -> str:
        """将 dict/list/... 序列化成 JSON 字符串，asyncpg 才能正确写入 jsonb 列。"""
        if isinstance(value, str):
            # 已经是 JSON 字符串，尝试 parse 一下确保合法后再 dumps，避免原始内容格式不规范
            try:
                json.loads(value)
                return value
            except (ValueError, TypeError):
                # 不是合法 JSON，兜底包成字符串
                return json.dumps(value)
        return json.dumps(value, ensure_ascii=False)

    async def save_message(self, event: dict) -> int:
        """
        落库一条 message。返回实际写入的 sequence_id。

        如果因为 ON CONFLICT DO NOTHING 没写入（Kafka 重放），
        返回 None。
        """

        if not self.pool:
            raise RuntimeError(
                "PostgreSQL pool is not started"
            )

        # Kafka JSON 传输后所有字段都是 str，这里按数据库列类型做显式转换
        message_id = self._to_uuid(event["message_id"])
        conversation_id = self._to_uuid(event["conversation_id"])
        user_id = self._to_int(event["user_id"])
        created_at = self._to_datetime(event["timestamp"])
        metadata = self._to_jsonb(event.get("metadata", {}))

        sequence_id: int | None = None

        # 三者放同一事务，保证 FK + 数据一致性
        # asyncpg 的 transaction() 在 Connection 上，不是 Pool 上
        async with self.pool.acquire() as conn:
            async with conn.transaction():

                # 1) 确保 users 表里有 id = user_id 的记录
                #    conversations.user_id → users.id 是 FK，缺了就会报外键冲突
                await conn.execute(
                    """
                    INSERT INTO users (id, user_uuid)
                    VALUES ($1, $2)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    user_id,
                    _uuid.uuid4(),
                )

                # 2) 确保 conversations 表里有 conversation_id 的记录
                #    已存在 → 更新 last_message_at / updated_at
                #    不存在 → 新建会话（title/status/message_count/created_at 走默认值）
                #
                # ★ 不再维护 conversations.message_count：
                #   - 旧代码：每次重放 event 都会 +1，导致计数虚高
                #   - 新方案：message_count 按需查询计算
                #     SELECT COUNT(*) FROM messages WHERE conversation_id = ...
                await conn.execute(
                    """
                    INSERT INTO conversations (
                        conversation_id,
                        user_id,
                        last_message_at
                    )
                    VALUES ($1, $2, $3)
                    ON CONFLICT (conversation_id) DO UPDATE SET
                        last_message_at  = EXCLUDED.last_message_at,
                        updated_at       = now()
                    """,
                    conversation_id,
                    user_id,
                    created_at,
                )

                # 3) 写入 messages
                #    sequence_id 用同事务内子查询原子计算并 RETURNING 拿回来
                #    ON CONFLICT (message_id) DO NOTHING 保证 Kafka 重放幂等
                #
                #    ★ 必须是 DO NOTHING，不能写
                #      "DO UPDATE SET message_id = EXCLUDED.message_id"：
                #      那是一句空转 UPDATE，会让 RETURNING 返回**旧行**的
                #      sequence_id，上层据此 push 到 Redis → 重放时同一条消息
                #      在 Redis List 里出现两次（最近 20 条被污染）。
                #      DO NOTHING 时冲突行不返回任何内容 → row 为 None →
                #      走下面的兜底回查拿真实的 sequence_id。
                row = await conn.fetchrow(
                    """
                    INSERT INTO messages (
                        message_id,
                        conversation_id,
                        user_id,
                        role,
                        content,
                        sequence_id,
                        created_at,
                        metadata
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        (SELECT COALESCE(MAX(sequence_id), 0) + 1
                         FROM messages
                         WHERE conversation_id = $2),
                        $6, $7
                    )
                    ON CONFLICT (message_id) DO NOTHING
                    RETURNING sequence_id
                    """,
                    message_id,
                    conversation_id,
                    user_id,
                    event.get("role", "user"),
                    event["content"],
                    created_at,
                    metadata,
                )

                # 冲突（重放同一条 event）时上面的 INSERT 不返回行 → row 为 None，
                # 回查已有行的 sequence_id 返回给上层做幂等判断。
                if row:
                    sequence_id = int(row["sequence_id"])
                else:
                    # 兜底：直接查当前 message_id 的 sequence_id
                    row2 = await conn.fetchrow(
                        "SELECT sequence_id FROM messages WHERE message_id = $1",
                        message_id,
                    )
                    if row2:
                        sequence_id = int(row2["sequence_id"])

        logger.info(
            "MessageRepository.save_message: message_id=%s, sequence_id=%s",
            event["message_id"], sequence_id,
        )
        return sequence_id