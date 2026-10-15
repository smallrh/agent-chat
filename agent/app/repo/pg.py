"""全局共享的 PostgreSQL 连接池。

## 为什么要这个模块

改造前，每个仓储组件各自 `asyncpg.create_pool()` 建一个独立连接池：

    auth(自己的小池) / chat_service._msg_repo / conversation_repo /
    consumer.message_repo / memory_worker.memory_repo / retrieval.memory_repo /
    profile.memory_repo / task_service._repo / outbox_relay._message_repo

PG 侧连接数 ≈ 9 × PG_POOL_SIZE，而 `RELOAD=true` 热重载会同时存在新旧进程，
连接数还要再翻倍 —— 这正是「连接数打满 / 连接假死」的根因之一。

## 现在的模型

**一个进程只有一个 PG 连接池**，所有仓储共享：

    from app.repo.pg import get_pool
    self.pool = await get_pool()

配套约定：
  - `get_pool()` 幂等且惰性创建（内部有 asyncio.Lock，并发首调只建一次）。
  - 仓储的 `start()` 保持幂等（已有池就直接用），因此
    「ProfileService 复用 MemoryWorker 的 repo」这类共享不会再被偷换成新池。
  - 仓储的 `stop()` **只解引用、不关池** —— 某个组件关闭时不能掐掉别人的连接；
    真正的关闭由 `app/main.py` 的 lifespan shutdown 调一次 `close_pool()` 完成。

## 池容量

统一用 `settings.PG_POOL_SIZE`。共享后总连接数从 9×N 降到 1×N，
若后续需要给热点路径保底，可以在这里按业务拆分「读写池」而不是每组件各建一个。
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.core.config import settings


logger = logging.getLogger(__name__)


_pool: asyncpg.Pool | None = None
# 并发首次调用时保证只创建一个池（asyncio.Lock 自 3.10 起不在创建时绑定事件循环）
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """惰性创建 Lock：避免模块导入时拿到「另一个事件循环」的锁。"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def get_pool() -> asyncpg.Pool:
    """返回进程级共享 PG 连接池（幂等、惰性创建）。"""
    global _pool

    if _pool is not None:
        return _pool

    async with _get_lock():
        # 双检：可能在等锁期间已被别人建好
        if _pool is None:
            _pool = await asyncpg.create_pool(
                host=settings.PG_HOST,
                port=settings.PG_PORT,
                database=settings.PG_DATABASE,
                user=settings.PG_USER,
                password=settings.PG_PASSWORD,
                min_size=1,
                max_size=settings.PG_POOL_SIZE,
            )
            logger.info(
                "PG shared pool created (max_size=%s)", settings.PG_POOL_SIZE,
            )
    return _pool


async def close_pool() -> None:
    """关闭共享池（幂等）。由 lifespan shutdown 调用一次。"""
    global _pool

    pool, _pool = _pool, None
    if pool is None:
        return
    try:
        await pool.close()
        logger.info("PG shared pool closed")
    except Exception:
        logger.exception("PG shared pool close failed (ignored)")


def current_pool() -> asyncpg.Pool | None:
    """当前共享池（未创建则为 None）。用于健康检查 / 诊断脚本。"""
    return _pool
