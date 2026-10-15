import json
import uuid as _uuid
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import HTTPException, status

from app.repo.pg import get_pool


class AttachmentRepository:
    """
    attachments 表操作。

    约定与 TaskRepository / MessageRepository 完全一致：
      - 共享连接池（app/repo/pg.py），start() 幂等
      - 写操作走单条 SQL（落盘由 service 层先做，再写元数据）
      - 读出来统一转 dict，UUID / datetime 转字符串
    """

    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def start(self):
        if self.pool is not None:
            return
        self.pool = await get_pool()

    async def stop(self):
        # 共享池不在组件级关闭，只解引用
        self.pool = None

    # ----------------------------------------------------------------
    # 类型转换
    # ----------------------------------------------------------------

    @staticmethod
    def _to_uuid(value) -> _uuid.UUID:
        if isinstance(value, _uuid.UUID):
            return value
        return _uuid.UUID(str(value))

    @staticmethod
    def _to_int(value) -> int:
        if isinstance(value, int):
            return value
        return int(value)

    # ----------------------------------------------------------------
    # 写入
    # ----------------------------------------------------------------

    async def create(
        self,
        attachment_id: str,
        user_id: int,
        name: str,
        mime: str,
        size_bytes: int,
        storage_path: str | None,
        extracted_text: str | None,
        conversation_id: str | None = None,
    ) -> str:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        await self.pool.execute(
            """
            INSERT INTO attachments (
                attachment_id, user_id, conversation_id, name, mime,
                size_bytes, storage_path, extracted_text, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
            """,
            self._to_uuid(attachment_id),
            self._to_int(user_id),
            self._to_uuid(conversation_id) if conversation_id else None,
            name[:255],
            mime[:120] or "application/octet-stream",
            int(size_bytes),
            storage_path,
            extracted_text,
        )
        return attachment_id

    async def bind_message(
        self,
        attachment_id: str,
        user_id: int,
        message_id: str,
        conversation_id: str | None = None,
    ) -> bool:
        """
        把附件绑定到真正发出的那条消息上（并补 conversation_id）。

        返回 False = 附件不存在或不属于该用户（防止跨用户绑定）。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        res = await self.pool.execute(
            """
            UPDATE attachments
            SET message_id = $3,
                conversation_id = COALESCE(conversation_id, $4),
                created_at = created_at
            WHERE attachment_id = $1 AND user_id = $2
            """,
            self._to_uuid(attachment_id),
            self._to_int(user_id),
            self._to_uuid(message_id) if message_id else None,
            self._to_uuid(conversation_id) if conversation_id else None,
        )
        n = int(res.split()[-1]) if res else 0
        return n > 0

    # ----------------------------------------------------------------
    # 读取
    # ----------------------------------------------------------------

    async def get(self, attachment_id: str) -> dict | None:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        row = await self.pool.fetchrow(
            "SELECT * FROM attachments WHERE attachment_id = $1",
            self._to_uuid(attachment_id),
        )
        return self._row_to_dict(row)

    async def get_for_user(self, attachment_id: str, user_id: int) -> dict | None:
        """带归属校验的读取。不属于该用户时抛 404（不泄露存在性）。"""
        row = await self.get(attachment_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found",
            )
        if int(row["user_id"]) != int(user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found",
            )
        return row

    async def list_for_user(
        self,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        rows = await self.pool.fetch(
            """
            SELECT * FROM attachments
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            self._to_int(user_id), limit, offset,
        )
        return [self._row_to_dict(r) for r in rows]

    # ----------------------------------------------------------------
    # 工具
    # ----------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k, v in list(d.items()):
            if isinstance(v, _uuid.UUID):
                d[k] = str(v)
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
        # attachments 表里没有 jsonb 列，这里保留 json 导入仅为对齐
        # 其它 repo 的写法（未来若加 metadata 列可直接用）。
        _ = json
        return d
