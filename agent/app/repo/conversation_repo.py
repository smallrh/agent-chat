"""会话读写（归属校验、列表、消息历史、软删）。"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from fastapi import HTTPException, status

from app.repo.message_repo import MessageRepository


class ConversationRepository:
    def __init__(self, msg_repo: MessageRepository):
        self._msg = msg_repo

    @property
    def pool(self):
        return self._msg.pool

    async def start(self):
        if self._msg.pool is None:
            await self._msg.start()

    async def stop(self):
        # PG 走共享池，这里只解引用委托给 MessageRepository（同样不关池）
        await self._msg.stop()

    @staticmethod
    def _iso(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.isoformat()

    def _to_item(self, r) -> dict:
        return {
            "conversation_id": str(r["conversation_id"]),
            "title": r["title"] or "新对话",
            "status": int(r["status"]),
            "last_message_at": self._iso(r["last_message_at"]),
            "created_at": self._iso(r["created_at"]),
            "updated_at": self._iso(r["updated_at"]),
        }

    async def list_conversations(
        self,
        user_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        rows = await self.pool.fetch(
            """
            SELECT conversation_id, title, status, last_message_at, created_at, updated_at
            FROM conversations
            WHERE user_id = $1 AND status = 1
            ORDER BY COALESCE(last_message_at, created_at) DESC
            LIMIT $2 OFFSET $3
            """,
            user_id,
            limit,
            offset,
        )
        return [self._to_item(r) for r in rows]

    async def get_conversation(self, conversation_id: str, user_id: int) -> dict | None:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        row = await self.pool.fetchrow(
            """
            SELECT conversation_id, title, status, last_message_at, created_at, updated_at, user_id
            FROM conversations
            WHERE conversation_id = $1
            """,
            _uuid.UUID(conversation_id),
        )
        if row is None:
            return None
        if int(row["user_id"]) != user_id or int(row["status"]) != 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conversation does not belong to current user.",
            )
        return self._to_item(row)

    async def list_messages(
        self,
        conversation_id: str,
        user_id: int,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        conv = await self.get_conversation(conversation_id, user_id)
        if conv is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )
        rows = await self.pool.fetch(
            """
            SELECT message_id, role, content, sequence_id, created_at, metadata
            FROM messages
            WHERE conversation_id = $1
            ORDER BY sequence_id ASC
            LIMIT $2 OFFSET $3
            """,
            _uuid.UUID(conversation_id),
            limit,
            offset,
        )
        items = []
        for r in rows:
            meta = r["metadata"]
            if hasattr(meta, "as_dict"):
                meta = meta.as_dict()
            items.append(
                {
                    "message_id": str(r["message_id"]),
                    "role": r["role"],
                    "content": r["content"] or "",
                    "sequence_id": int(r["sequence_id"]),
                    "created_at": self._iso(r["created_at"]),
                    "metadata": meta if isinstance(meta, dict) else {},
                }
            )
        return items

    async def update_conversation_title(
        self,
        conversation_id: str,
        user_id: int,
        title: str,
    ) -> dict:
        conv = await self.get_conversation(conversation_id, user_id)
        if conv is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )
        await self.pool.execute(
            """
            UPDATE conversations
            SET title = $2, updated_at = now()
            WHERE conversation_id = $1 AND user_id = $3 AND status = 1
            """,
            _uuid.UUID(conversation_id),
            title[:255],
            user_id,
        )
        conv["title"] = title[:255]
        return conv

    async def set_title_if_empty(
        self,
        conversation_id: str,
        user_id: int,
        title: str,
    ) -> None:
        if not self.pool:
            return
        await self.pool.execute(
            """
            UPDATE conversations
            SET title = $2, updated_at = now()
            WHERE conversation_id = $1 AND user_id = $3
              AND status = 1
              AND (title IS NULL OR title = '')
            """,
            _uuid.UUID(conversation_id),
            title[:255],
            user_id,
        )

    async def soft_delete_conversation(self, conversation_id: str, user_id: int) -> None:
        conv = await self.get_conversation(conversation_id, user_id)
        if conv is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )
        await self.pool.execute(
            """
            UPDATE conversations
            SET status = 0, updated_at = now()
            WHERE conversation_id = $1 AND user_id = $2
            """,
            _uuid.UUID(conversation_id),
            user_id,
        )
