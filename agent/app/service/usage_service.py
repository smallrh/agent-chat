"""用量埋点（P3 计费/配额）。

## 定位
这一层**只负责计数**，不做「是否允许」的判断 —— 配额判定在 Go 网关
（`QuotaMiddleware`），因为拦截必须发生在请求进入 Agent 之前，
等到 Agent 这边再判已经消耗完资源了。

## 为什么用 Redis 而不是直接写 PG
计费计数是高频写（每轮对话 +1），直接落 PG 会把连接池和 WAL 打满。
Redis `INCR` 是原子自增，天然适合计数；按天/按月两个窗口各存一份，
都带 TTL，过期自动清理 —— 不需要额外的清理任务。

## 故障策略：一律放行
和已有 `rate_limit_service` 保持同一原则：**Redis 挂了不能让用户聊不了天**。
埋点失败只记日志，绝不向上抛 —— 丢几次计数（少收钱）远好过
「因为计数失败导致整个对话 500」（用户流失）。真要对账，
后面可以再补 PG 的准实时落库（outbox 同款思路）。

## Key 规则（Go 侧必须一致）
    usage:{metric}:{user_id}:{YYYYMMDD}      日窗口，TTL 2 天
    usage:m:{metric}:{user_id}:{YYYYMM}      月窗口，TTL 45 天

metric 取值：chat（对话轮次）/ web_search（联网检索）/ task（后台任务）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.repo.redis_repo import RedisRepository


logger = logging.getLogger(__name__)

# 计量维度（与 Go 侧 internal/service/quota_service.go 保持一致）
METRIC_CHAT = "chat"
METRIC_SEARCH = "web_search"
METRIC_TASK = "task"

ALL_METRICS = (METRIC_CHAT, METRIC_SEARCH, METRIC_TASK)

_DAY_TTL = 2 * 24 * 3600
_MONTH_TTL = 45 * 24 * 3600


def day_key(metric: str, user_id: int | str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"usage:{metric}:{user_id}:{day}"


def month_key(metric: str, user_id: int | str) -> str:
    month = datetime.now(timezone.utc).strftime("%Y%m")
    return f"usage:m:{metric}:{user_id}:{month}"


class UsageService:
    def __init__(self):
        self._redis = RedisRepository()
        self._started = False

    async def start(self):
        if not self._started:
            await self._redis.start()
            self._started = True

    async def aclose(self):
        if self._started and self._redis is not None:
            try:
                await self._redis.stop()
            except Exception:
                logger.exception("UsageService Redis close failed (ignored)")
            self._started = False

    async def _ensure(self):
        if not self._started:
            await self.start()

    # ----------------------------------------------------------------
    # 写
    # ----------------------------------------------------------------

    async def incr(self, user_id: int | str, metric: str, n: int = 1) -> None:
        """累加用量。失败只记日志，绝不抛（见文件头「故障策略」）。"""
        if metric not in ALL_METRICS:
            logger.warning("UsageService.incr unknown metric: %s", metric)
            return
        try:
            await self._ensure()
            client = self._redis.client
            if client is None:
                return
            dk, mk = day_key(metric, user_id), month_key(metric, user_id)
            pipe = client.pipeline()
            pipe.incrby(dk, n)
            pipe.expire(dk, _DAY_TTL)
            pipe.incrby(mk, n)
            pipe.expire(mk, _MONTH_TTL)
            await pipe.execute()
        except Exception:
            logger.exception(
                "UsageService.incr FAILED (ignored): user=%s metric=%s", user_id, metric
            )

    # ----------------------------------------------------------------
    # 读（主要给自检 / 排查用；对外查询走 Go 的 /api/v1/usage/me）
    # ----------------------------------------------------------------

    async def get_day(self, user_id: int | str, metric: str) -> int:
        try:
            await self._ensure()
            client = self._redis.client
            if client is None:
                return 0
            raw = await client.get(day_key(metric, user_id))
            return int(raw) if raw else 0
        except Exception:
            logger.exception("UsageService.get_day FAILED: user=%s", user_id)
            return 0

    async def get_month(self, user_id: int | str, metric: str) -> int:
        try:
            await self._ensure()
            client = self._redis.client
            if client is None:
                return 0
            raw = await client.get(month_key(metric, user_id))
            return int(raw) if raw else 0
        except Exception:
            logger.exception("UsageService.get_month FAILED: user=%s", user_id)
            return 0

    async def snapshot(self, user_id: int | str) -> dict:
        """某一用户的三项用量（日 / 月）。"""
        out: dict = {"user_id": str(user_id), "day": {}, "month": {}}
        for m in ALL_METRICS:
            out["day"][m] = await self.get_day(user_id, m)
            out["month"][m] = await self.get_month(user_id, m)
        return out


usage_service = UsageService()
