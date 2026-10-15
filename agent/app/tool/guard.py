"""工具执行护栏（guard）：超时 / 重试 / 熔断 / 配额 / 审计。

定位：ToolExecutor 负责「参数装配」，ToolGuard 负责「怎么安全地跑」。

```
ToolExecutor.execute()       # 同步入口 → Agent.chat（非流式）使用
    → ToolGuard.run()                  ← 阻塞等待，内部走线程池

ToolExecutor.aexecute()      # 异步入口 → Agent.stream（流式）使用
    → await ToolGuard.arun()          ← 不阻塞事件循环
```

## 为什么两个入口

- 非流式 `Agent.chat()` 是**同步**方法（跑在 `asyncio.to_thread` 里），只能阻塞等工具 → `run()`
- 流式 `Agent.stream()` 是 async，直接 `await` → `arun()`。
  若这里也用 `run()` + `to_thread`，**每个工具调用会占用 2 个线程**
  （一层 to_thread 阻塞 + 一层 guard 线程池），并发时线程池消耗翻倍。

## 同步工具超时的硬限制

CPython **无法强杀线程**。这里实现的超时只是「调用方不再等待，立刻返回错误文案」，
线程里的代码仍会跑到自然结束。因此**工具必须自带资源上限**
（code_runner 的 subprocess timeout、httpx 的 timeout…），guard 只是外部兜底。

## 熔断 / 配额的作用域

状态是**进程内**的。多副本时各副本独立计数，等效阈值 × 副本数。
要跨副本一致就把 `_CIRCUIT` / `_QUOTA` 的后端换成 Redis，调用方代码不用动。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.core.obs import (
    M_TOOL_CALLS,
    M_TOOL_DURATION,
    metrics,
    trace_scope,
)
from app.tool.context import ToolContext, tool_context_scope
from app.tool.spec import ToolMeta

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass
class GuardResult:
    """一次受保护调用的结果。"""

    ok: bool
    text: str                   # 成功 = 工具返回值；失败 = 可直接喂 LLM 的错误文案
    attempts: int = 0
    elapsed_ms: int = 0
    blocked: str | None = None  # disabled / circuit / quota；None 表示真的执行了


# ---------------------------------------------------------------------------
# 进程内状态
# ---------------------------------------------------------------------------


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float = 0.0      # 单调时间戳；0 = 未打开

    def is_open(self, threshold: int, reset: float) -> bool:
        if self.opened_at <= 0:
            return False
        if time.monotonic() - self.opened_at >= reset:
            # 冷却结束 → 半开，放一次请求探活
            self.opened_at = 0.0
            self.failures = 0
            return False
        return True


_CIRCUIT: dict[str, _Circuit] = {}
# (tool_name, user_id) -> 最近调用时间戳队列
_QUOTA: dict[tuple[str, Any], deque[float]] = {}
_state_lock = threading.Lock()

# 同步工具线程池（懒创建）
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            workers = max(4, int(getattr(settings, "TOOL_EXECUTOR_WORKERS", 8)))
            _executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="tool-guard",
            )
        return _executor


def _quota_exceeded(name: str, user_key: Any, cfg) -> bool:
    limit = cfg.quota_per_minute
    if limit <= 0:
        return False

    key = (name, user_key)
    now = time.monotonic()
    window = 60.0
    with _state_lock:
        bucket = _QUOTA.setdefault(key, deque())
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False


def _circuit_open(name: str, cfg) -> bool:
    with _state_lock:
        state = _CIRCUIT.get(name)
        if state is None:
            return False
        return state.is_open(cfg.circuit_threshold, cfg.circuit_reset)


def _describe(exc: BaseException, kind: str = "") -> str:
    """把异常格式化成可直接喂回 LLM 的可读文案。"""
    name = type(exc).__name__
    msg = str(exc)
    if kind:
        return f"{name}（{kind}）：{msg}"
    return f"{name}：{msg}"


def _brief_kwargs(kwargs: dict[str, Any], with_values: bool) -> str:
    """参数摘要：默认只给键名与长度，避免把用户内容打进日志。"""
    if not kwargs:
        return "{}"
    parts: list[str] = []
    for key, value in kwargs.items():
        if with_values:
            parts.append(f"{key}={value!r}"[:80])
        else:
            parts.append(f"{key}:{len(str(value))}")
    return "{" + ", ".join(parts) + "}"


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


class ToolGuard:
    """执行策略：把一次工具调用包在超时/重试/熔断/配额/审计里。"""

    # ------------------------------------------------------------------
    # 同步入口（Agent.chat / 非流式）
    # ------------------------------------------------------------------

    def run(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> GuardResult:
        """阻塞式执行（调用方已在 worker 线程里）。"""
        cfg = meta.config
        started = time.perf_counter()

        blocked = self._precheck(meta, cfg, ctx)
        if blocked:
            return self._blocked(blocked, meta, started)

        result = self._invoke_with_retry(meta, kwargs, ctx)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._record_and_audit(meta, kwargs, ctx, result)
        return result

    # ------------------------------------------------------------------
    # 异步入口（Agent.stream / 流式）
    # ------------------------------------------------------------------

    async def arun(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> GuardResult:
        """非阻塞执行：等待期间让出事件循环，不额外占用 to_thread 线程。"""
        cfg = meta.config
        started = time.perf_counter()

        blocked = self._precheck(meta, cfg, ctx)
        if blocked:
            return self._blocked(blocked, meta, started)

        result = await self._ainvoke_with_retry(meta, kwargs, ctx)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._record_and_audit(meta, kwargs, ctx, result)
        return result

    # ------------------------------------------------------------------
    # 前置拦截（两条路径共用）
    # ------------------------------------------------------------------

    @staticmethod
    def _precheck(meta: ToolMeta, cfg, ctx: ToolContext) -> str | None:
        """返回拦截原因（disabled / quota / circuit）；None = 放行。"""
        if not cfg.enabled:
            return "disabled"

        user_key = ctx.user_id if ctx.user_id is not None else "__anonymous__"
        if _quota_exceeded(meta.name, user_key, cfg):
            return "quota"

        if _circuit_open(meta.name, cfg):
            return "circuit"

        return None

    # ------------------------------------------------------------------
    # 单次调用 + 超时
    # ------------------------------------------------------------------

    def _invoke_once(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[bool, Any, str]:
        """同步路径：在 guard 线程池执行，future.result(timeout) 控时。"""
        try:
            if meta.is_async and ctx.loop is not None:
                future = asyncio.run_coroutine_threadsafe(
                    self._async_call(meta, kwargs, ctx), ctx.loop,
                )
            elif meta.is_async:
                return False, None, (
                    "工具需要异步事件循环，但当前未提供（loop=None）"
                )
            else:
                future = _get_executor().submit(
                    self._sync_call, meta, kwargs, ctx,
                )
            return True, future.result(timeout=meta.config.timeout), ""
        except Exception as exc:                            # noqa: BLE001
            kind = "超时" if isinstance(exc, TimeoutError) else ""
            return False, None, _describe(exc, kind)

    async def _ainvoke_once(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[bool, Any, str]:
        """异步路径：执行仍在其它线程，await 期间让出事件循环。"""
        loop = asyncio.get_running_loop()

        if meta.is_async:
            if ctx.loop is None:
                return False, None, (
                    "工具需要异步事件循环，但当前未提供（loop=None）"
                )
            awaitable = asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(
                    self._async_call(meta, kwargs, ctx), ctx.loop
            )
            )
        else:
            awaitable = loop.run_in_executor(
                _get_executor(), self._sync_call, meta, kwargs, ctx
        )

        try:
            value = await asyncio.wait_for(
                awaitable, timeout=meta.config.timeout
        )
            return True, value, ""
        except asyncio.TimeoutError as exc:
            return False, None, _describe(exc, "超时")
        except Exception as exc:                            # noqa: BLE001
            return False, None, _describe(exc)

    # ------------------------------------------------------------------
    # 重试
    # ------------------------------------------------------------------

    def _invoke_with_retry(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> GuardResult:
        cfg = meta.config
        attempts = cfg.retries + 1
        last_error = ""

        for attempt in range(1, attempts + 1):
            ok, value, error = self._invoke_once(meta, kwargs, ctx)
            if ok:
                self._record_success(meta.name)
                return GuardResult(ok=True, text=str(value), attempts=attempt)

            last_error = error or "未知错误"
            self._record_failure(meta.name, cfg)

            if attempt < attempts:
                delay = min(
                    cfg.retry_backoff * (2 ** (attempt - 1)),
                    cfg.retry_max_backoff,
                )
                logger.warning(
                    "tool retry %d/%d after %.2fs: name=%s err=%s",
                    attempt, attempts, meta.name, last_error[:200],
                )
                time.sleep(delay)

        return GuardResult(
            ok=False,
            text=f"工具执行失败（已重试{attempts - 1}次）：{last_error}",
            attempts=attempts,
        )

    async def _ainvoke_with_retry(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> GuardResult:
        cfg = meta.config
        attempts = cfg.retries + 1
        last_error = ""

        for attempt in range(1, attempts + 1):
            ok, value, error = await self._ainvoke_once(meta, kwargs, ctx)
            if ok:
                self._record_success(meta.name)
                return GuardResult(ok=True, text=str(value), attempts=attempt)

            last_error = error or "未知错误"
            self._record_failure(meta.name, cfg)

            if attempt < attempts:
                delay = min(
                    cfg.retry_backoff * (2 ** (attempt - 1)),
                    cfg.retry_max_backoff,
                )
                logger.warning(
                    "tool retry %d/%d after %.2fs: name=%s err=%s",
                    attempt, attempts, meta.name, last_error[:200],
                )
                await asyncio.sleep(delay)

        return GuardResult(
            ok=False,
            text=f"工具执行失败（已重试{attempts - 1}次）：{last_error}",
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # 调用包装：重建 ToolContext 与 trace_id 作用域
    # ------------------------------------------------------------------

    @staticmethod
    def _sync_call(meta, kwargs: dict, ctx: ToolContext):
        """同步工具：线程池线程**不会**继承调用方的 contextvars，
        必须显式重建作用域，否则工具内部 get_tool_context() 拿到空上下文。
        """
        with trace_scope(ctx.trace_id), tool_context_scope(ctx):
            return meta.impl(**kwargs)

    @staticmethod
    async def _async_call(meta, kwargs: dict, ctx: ToolContext):
        """异步工具：协程桥接到主事件循环后同样丢失 contextvars，一并恢复。"""
        with trace_scope(ctx.trace_id), tool_context_scope(ctx):
            return await meta.impl(**kwargs)

    @staticmethod
    def _blocked(reason: str, meta: ToolMeta, started: float) -> GuardResult:
        """构造一个「被拦截」的结果（不执行工具，直接给模型可读文案）。"""
        text = {
            "disabled": f"工具已停用：{meta.name}",
            "quota": f"工具调用过于频繁，请稍后再试：{meta.name}",
            "circuit": f"工具暂时不可用（连续失败过多，已自动熔断）：{meta.name}",
        }[reason]
        result = GuardResult(ok=False, text=text, attempts=0, blocked=reason)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "tool blocked: name=%s reason=%s elapsed_ms=%d",
            meta.name, reason, result.elapsed_ms,
        )
        return result

    # ------------------------------------------------------------------
    # 后置：状态更新 + 审计（两条路径共用，避免逻辑漂移）
    # ------------------------------------------------------------------

    @staticmethod
    def _record_and_audit(
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
        result: GuardResult,
    ) -> None:
        if result.blocked:
            return  # 被拦截的调用没真正执行，不记失败也不记耗时分布

        status = "ok" if result.ok else (result.blocked or "error")
        metrics.inc(M_TOOL_CALLS, 1, tool=meta.name, status=status)
        metrics.observe(M_TOOL_DURATION, result.elapsed_ms, tool=meta.name)
        ToolGuard._audit(meta, ctx, kwargs, result)

    # ------------------------------------------------------------------

    @staticmethod
    def _record_success(name: str) -> None:
        with _state_lock:
            _CIRCUIT.pop(name, None)

    @staticmethod
    def _record_failure(name: str, cfg) -> None:
        with _state_lock:
            state = _CIRCUIT.setdefault(name, _Circuit())
            state.failures += 1
            if state.failures >= cfg.circuit_threshold and state.opened_at <= 0:
                state.opened_at = time.monotonic()
                logger.error(
                    "tool circuit OPEN: name=%s failures=%d cooldown=%.0fs",
                name, state.failures, cfg.circuit_reset,
            )

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    @staticmethod
    def _audit(
        meta: ToolMeta,
        ctx: ToolContext,
        kwargs: dict[str, Any],
        result: GuardResult,
    ) -> None:
        """结构化审计日志。

        默认**不落参数内容**（可能含用户隐私）。需要排障时把
        `TOOL_AUDIT_LOG_ARGS` 打开，此时也只记到 debug 级别。
        """
        logger.info(
            "tool.audit | name=%s user_id=%s conv=%s ok=%s attempts=%d "
            "elapsed_ms=%d args=%s result_len=%d",
            meta.name, ctx.user_id, ctx.conversation_id,
            result.ok, result.attempts, result.elapsed_ms,
            _brief_kwargs(kwargs, with_values=False),
            len(result.text),
        )

        if getattr(settings, "TOOL_AUDIT_LOG_ARGS", False):
            logger.debug(
                "tool.audit.detail | name=%s args=%s result=%.300s",
                meta.name, _brief_kwargs(kwargs, with_values=True), result.text,
            )

    # ------------------------------------------------------------------
    # 运维接口
    # ------------------------------------------------------------------

    @staticmethod
    def reset(name: str | None = None) -> None:
        """手动清除熔断 / 配额状态（排障用）。"""
        with _state_lock:
            if name is None:
                _CIRCUIT.clear()
                _QUOTA.clear()
            else:
                _CIRCUIT.pop(name, None)
                for key in [k for k in _QUOTA if k[0] == name]:
                    _QUOTA.pop(key, None)

    @staticmethod
    def stats() -> dict[str, Any]:
        """当前熔断 / 配额状态快照。"""
        now = time.monotonic()
        with _state_lock:
            circuits = {
                name: {
                    "failures": st.failures,
                    "open": st.is_open(
                        int(getattr(settings, "TOOL_CIRCUIT_THRESHOLD", 5)),
                        float(getattr(settings, "TOOL_CIRCUIT_RESET_SECONDS", 60.0)),
                    ),
                    "remaining_cooldown": (
                        max(0, int(
                            float(getattr(settings, "TOOL_CIRCUIT_RESET_SECONDS", 60.0))
                            - (now - st.opened_at)
                        ))
                        if st.opened_at > 0 else 0
                    ),
                }
                for name, st in _CIRCUIT.items()
            }
            quota_keys = len(_QUOTA)
        return {"circuits": circuits, "quota_keys": quota_keys}


tool_guard = ToolGuard()
