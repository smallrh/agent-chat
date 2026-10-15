import json
import uuid as _uuid
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import HTTPException, status

from app.core.config import settings
from app.repo.pg import get_pool


# tasks.status 常量（与 SQL 默认值 / 校验保持一致）
TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

_VALID_STATUSES = {
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED,
    TASK_STATUS_CANCELLED,
}


class TaskRepository:
    """
    tasks 表操作。

    完全照搬 MessageRepository 的约定：
      - 共享连接池（app/repo/pg.py），start() 幂等
      - _to_uuid / _to_int / _to_datetime / _to_jsonb 类型转换
      - start()/stop() 生命周期
      - 写操作走事务保证一致性
    """

    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    # ----------------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------------

    async def start(self):
        """取共享连接池（幂等，已有池直接复用）。见 app/repo/pg.py。"""
        if self.pool is not None:
            return
        self.pool = await get_pool()

    async def stop(self):
        # 共享池不在这里关闭（别的组件还在用），只解引用。
        self.pool = None

    # ----------------------------------------------------------------
    # 类型转换（和 MessageRepository 同款，asyncpg 强类型需要）
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

    @staticmethod
    def _to_datetime(value) -> datetime:
        if isinstance(value, datetime):
            return value
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)

    @staticmethod
    def _to_jsonb(value) -> str:
        if isinstance(value, str):
            try:
                json.loads(value)
                return value
            except (ValueError, TypeError):
                return json.dumps(value, ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)

    # ----------------------------------------------------------------
    # conversation 归属校验（只读，不创建）
    # ----------------------------------------------------------------

    async def assert_conversation_owner(
        self,
        conversation_id: str,
        user_id: int,
    ) -> None:
        """
        校验 conversation_id 属于当前 user_id（只读校验，不创建会话）。

        和 MessageRepository.ensure_conversation_owner 的区别：
          - 这里用于 GET /tasks/stream 等「只读订阅」场景，
            会话不存在应 404 而不是自动创建。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        row = await self.pool.fetchrow(
            "SELECT user_id FROM conversations WHERE conversation_id = $1",
            self._to_uuid(conversation_id),
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Conversation {conversation_id} not found.",
            )
        if int(row["user_id"]) != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Conversation {conversation_id} does not belong to current user.",
            )

    # ----------------------------------------------------------------
    # 写入
    # ----------------------------------------------------------------

    async def create_task(
        self,
        task_id: str,
        conversation_id: str,
        user_id: int,
        title: str,
        goal: str,
        kind: str = "demo",
    ) -> str:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        await self.pool.execute(
            """
            INSERT INTO tasks (
                task_id, conversation_id, user_id, title, goal, kind,
                status, progress, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, 0, now(), now())
            """,
            self._to_uuid(task_id),
            self._to_uuid(conversation_id),
            self._to_int(user_id),
            title,
            goal,
            kind or "demo",
            TASK_STATUS_PENDING,
        )
        return task_id

    async def set_running(self, task_id: str, steps_total: int = 0) -> None:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        await self.pool.execute(
            """
            UPDATE tasks
            SET status = $2,
                steps_total = CASE WHEN $3 > 0 THEN $3 ELSE steps_total END,
                updated_at = now()
            WHERE task_id = $1
            """,
            self._to_uuid(task_id),
            TASK_STATUS_RUNNING,
            int(steps_total or 0),
        )

    async def update_progress(
        self,
        task_id: str,
        progress: int,
        step: str | None = None,
        steps_done: int | None = None,
        steps_total: int | None = None,
    ) -> None:
        """
        更新进度。step 为空表示「只更新百分比，不动步骤文案」。

        steps_done / steps_total 单独可空：worker 中途才知道总步数时，
        可以先只报 done，再报 total（反之亦然），不必一次性给全。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        pct = max(0, min(100, int(progress)))
        await self.pool.execute(
            """
            UPDATE tasks
            SET progress = $2,
                step = COALESCE($3, step),
                steps_done = COALESCE($4, steps_done),
                steps_total = COALESCE($5, steps_total),
                updated_at = now()
            WHERE task_id = $1
            """,
            self._to_uuid(task_id),
            pct,
            step[:255] if step else None,
            None if steps_done is None else int(steps_done),
            None if steps_total is None else int(steps_total),
        )

    async def finish_task(
        self,
        task_id: str,
        task_status: str,
        result: Any | None = None,
        error: str | None = None,
        step: str | None = None,
    ) -> None:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        if task_status not in _VALID_STATUSES:
            raise ValueError(f"invalid task status: {task_status}")

        await self.pool.execute(
            """
            UPDATE tasks
            SET status = $2,
                progress = CASE WHEN $2 = 'succeeded' THEN 100 ELSE progress END,
                result = $3::jsonb,
                error = $4,
                step = COALESCE($5, step),
                updated_at = now()
            WHERE task_id = $1
            """,
            self._to_uuid(task_id),
            task_status,
            self._to_jsonb(result) if result is not None else None,
            error[:500] if error else None,
            step[:255] if step else None,
        )

    async def recover_interrupted(self, limit: int = 500) -> list[dict]:
        """
        把上次进程遗留的未终态任务置为 failed，并 RETURNING 回来供上层发事件。

        背景：任务跑的是进程内 `asyncio.create_task`，进程一停（部署 / 崩溃 /
        RELOAD=true 热重载）那些协程就没了，但 DB 里那行还停在 pending/running，
        前端进度条会永远转圈。模式与 outbox_repo.recover_stale_processing 一致。

        注：假设单实例部署。若将来多副本，这里要加 owner_id/lease 过滤，
        否则会把别的实例正在跑的任务误判为中断。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        rows = await self.pool.fetch(
            """
            UPDATE tasks
            SET status = $1,
                error = $2,
                updated_at = now()
            WHERE task_id IN (
                SELECT task_id FROM tasks
                WHERE status IN ('pending', 'running')
                ORDER BY created_at
                LIMIT $3
                FOR UPDATE SKIP LOCKED
            )
            RETURNING task_id, conversation_id, user_id, title
            """,
            TASK_STATUS_FAILED,
            "interrupted by restart",
            limit,
        )
        return [self._row_to_dict(r) for r in rows]

    async def cancel(self, task_id: str, user_id: int) -> bool:
        """
        取消一个任务。仅当任务仍在进行中（pending/running）才允许。

        返回 True 表示成功置为 cancelled；False 表示任务已终态无法取消。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        result = await self.pool.execute(
            """
            UPDATE tasks
            SET status = $3, updated_at = now()
            WHERE task_id = $1
              AND user_id = $2
              AND status IN ('pending', 'running')
            """,
            self._to_uuid(task_id),
            self._to_int(user_id),
            TASK_STATUS_CANCELLED,
        )
        # asyncpg execute 返回 "UPDATE N"
        n = int(result.split()[-1]) if result else 0
        return n > 0

    # ----------------------------------------------------------------
    # 读取
    # ----------------------------------------------------------------

    async def get_task(self, task_id: str) -> dict | None:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        row = await self.pool.fetchrow(
            "SELECT * FROM tasks WHERE task_id = $1",
            self._to_uuid(task_id),
        )
        return self._row_to_dict(row)

    async def count_active(self, user_id: int | None = None) -> int:
        """
        统计未终态（pending/running）任务数。

        user_id 不传 = 全局；传了 = 该用户。用于 spawn 前的并发上限校验。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")
        if user_id is None:
            row = await self.pool.fetchrow(
                "SELECT count(*) AS c FROM tasks WHERE status IN ('pending','running')"
            )
        else:
            row = await self.pool.fetchrow(
                """
                SELECT count(*) AS c FROM tasks
                WHERE status IN ('pending','running') AND user_id = $1
                """,
                self._to_int(user_id),
            )
        return int(row["c"]) if row else 0

    async def list_user_tasks(
        self,
        user_id: int,
        status_filter: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        按用户跨会话列出任务（任务中心页用）。

        与 list_tasks 的区别：list_tasks 必须带 conversation_id（会话内面板），
        这里查的是「我全部会话的任务」。已带 FK ON DELETE CASCADE，
        会话被删后任务行也一并消失，不需要额外过滤孤儿。
        """
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        clauses = ["user_id = $1"]
        params: list[Any] = [self._to_int(user_id)]
        idx = 2
        if status_filter:
            clauses.append(f"status = ${idx}")
            params.append(status_filter)
            idx += 1
        if kind:
            clauses.append(f"kind = ${idx}")
            params.append(kind)
            idx += 1

        params.append(limit)
        params.append(offset)
        rows = await self.pool.fetch(
            f"""
            SELECT * FROM tasks
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
        return [self._row_to_dict(r) for r in rows]

    async def list_tasks(
        self,
        conversation_id: str,
        user_id: int | None = None,
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        if not self.pool:
            raise RuntimeError("PostgreSQL pool is not started")

        clauses = ["conversation_id = $1"]
        params: list[Any] = [self._to_uuid(conversation_id)]
        idx = 2
        if user_id is not None:
            clauses.append(f"user_id = ${idx}")
            params.append(self._to_int(user_id))
            idx += 1
        if status_filter:
            clauses.append(f"status = ${idx}")
            params.append(status_filter)
            idx += 1

        params.append(limit)
        params.append(offset)
        rows = await self.pool.fetch(
            f"""
            SELECT * FROM tasks
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
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
        # 把 UUID / datetime 转回字符串，方便 JSON 序列化
        for k, v in list(d.items()):
            if isinstance(v, _uuid.UUID):
                d[k] = str(v)
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
        # jsonb 列（result）在不同 asyncpg 配置下可能是 str 也可能是已解码对象。
        # 这里统一成「能直接塞进 Pydantic Any 字段」的形态：
        # str 就尝试反序列化，失败则原样保留。
        raw = d.get("result")
        if isinstance(raw, str):
            try:
                d["result"] = json.loads(raw)
            except (ValueError, TypeError):
                pass
        return d


# 注意：这里刻意【不提供】模块级单例 task_repo。
#
# 历史 bug：本文件曾有 `task_repo = TaskRepository()`，但 lifespan 只调用
# task_service.start()（它启动的是 TaskService 内部的另一个 TaskRepository），
# 模块级单例从未 start() → pool 为 None → /tasks 全部端点抛
# "PostgreSQL pool is not started" 并返回 500。
#
# 统一入口：需要访问 tasks 表时，用 TaskService 持有的实例 ——
#     from app.service.task_service import task_service
#     await task_service.repo.xxx(...)
# （API 层 app/api/tasks.py 已封装为 _repo()。）
