"""FastAPI 依赖注入 —— 用户身份解析。

Python Agent **不自己做认证**。认证由 Go Auth Service（:8080）完成，
Go Auth 代理请求时会在 HTTP Header 里注入：

    X-User-Id: <user_uuid>   # JWT.sub，UUID string，可信

Python Agent 只做两件事：
  1. 从 X-User-Id Header 读 user_uuid（UUID string）
  2. 查 users 表把 user_uuid 转成 users.id（int8）—— 内部存储用的 id

参考 13-Python Agent 服务规范.md 第 13 节 + 12-跨服务契约规范.md 第 2 节。
"""
from __future__ import annotations

import logging

import asyncpg
from fastapi import Depends, Header, HTTPException, status

from app.core.config import settings
from app.repo.pg import close_pool as close_shared_pool, get_pool


logger = logging.getLogger(__name__)


# --------------------------------------------------------------
# PG 连接池：直接用进程级共享池（app/repo/pg.py）
#
# 改造前这里自己建了一个「小池」，和其它仓储的池互不相干 —— 全进程一度有
# 9 个独立 asyncpg 池（PG 侧连接数 ≈ 9 × PG_POOL_SIZE，热重载再翻倍）。
# 现在统一共享一个池，由 lifespan shutdown 的 close_pool() 统一关闭。
# --------------------------------------------------------------

async def _get_pool() -> asyncpg.Pool:
    """返回全局共享 PG 连接池（幂等，见 app/repo/pg.py）。"""
    return await get_pool()


# --------------------------------------------------------------
# FastAPI 依赖：从 X-User-Id Header 拿到 users.id
# --------------------------------------------------------------

async def get_current_user_id(
    x_user_id: str = Header(
        ...,
        alias="X-User-Id",
        description="Go Auth Service 注入的 user_uuid（UUID string）。"
        "客户端自己传的 X-User-Id 在 Go Auth 代理层已被强制覆盖，此处可信。",
    ),
) -> int:
    """把 Go Auth 注入的 user_uuid 转成 Python Agent 内部的 users.id。

    FastAPI 自动作为依赖注入：
        @router.post("/chat")
        async def chat(
            request: ChatRequest,
            user_id: int = Depends(get_current_user_id),  # ← 这里
        ):
            ...

    Raises:
        HTTPException(401):  X-User-Id 缺失或为空。
        HTTPException(400):  X-User-Id 不是合法 UUID。
        HTTPException(404):  UUID 对应的 users 记录不存在（Go Auth 注册时
                             应该已调用 users 表的 EnsureUser，如果不存在说明
                             数据有一致性问题）。
        HTTPException(500):  DB 连接失败。
    """
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-User-Id header. 请确保请求经过 Go Auth Service 代理。",
        )

    # 校验 UUID 格式（不合法的话 Go Auth 那边早就拦了，但双保险）
    try:
        import uuid
        uuid.UUID(x_user_id.strip())
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid X-User-Id format: {x_user_id!r} (expected UUID)",
        )

    pool = await _get_pool()
    try:
        row = await pool.fetchrow(
            "SELECT id FROM users WHERE user_uuid = $1",
            x_user_id.strip(),
        )
    except Exception as exc:
        logger.exception("Failed to resolve user_uuid → users.id")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while resolving user identity",
        ) from exc

    if row is None:
        # 正常流程 Go Auth Register 里已调用 python_user_repo.EnsureUser 插入 users 记录。
        # 如果没找到，说明数据不一致（手动操作 DB / EnsureUser 失败）
        logger.error(
            "users.user_uuid not found — data consistency issue: user_uuid=%s",
            x_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found in users table. "
                   "This should not happen after a successful registration.",
        )

    user_id: int = row["id"]
    logger.info(
        "Resolved X-User-Id → users.id: user_uuid=%s → id=%d",
        x_user_id, user_id,
    )
    return user_id


async def close_pool() -> None:
    """lifespan shutdown 时调用 —— 关闭的是**共享池**，故只能进程退出时调一次。"""
    await close_shared_pool()
