"""修复「三连问很慢」的三个问题：

1. calculator：int 结果超过 CPython int_max_str_digits(默认 4300) 时 f"{value}" 会抛
   ValueError → 工具返回"计算失败" → 模型反复换工具重试，浪费大量 LLM 往返。
   改为主动降级返回可读提示，工具永远不因"结果太大"而失败。

2. web_search：Exa agent 模式实测 ~33s，而 timeout 只给了 30s → 必然超时；
   且 retries=1 会让它重试一次再吃 30s（总 60s）还最终失败。
   改为 timeout=60、retries=0：让请求能跑完，失败就快速降级不再翻倍。

3. 去掉双层线程池：Agent.stream 里的 asyncio.to_thread(execute) 会额外占用
   默认 executor 的一个线程去阻塞等待 guard 自己的线程池。
   加 ToolExecutor.aexecute()，stream 路径直接 await，每个工具只占 1 个线程。
"""

import io

# ============================================================ 1) calculator
p = "app/tool/calculator.py"
s = io.open(p, encoding="utf-8").read()

old = '''def calculator(expression: str) -> str:'''
new = '''# Python 3.11+ 对 int → str 有位数上限（sys.get_int_max_str_digits，默认 4300）。
# 22**5000 有 6743 位，直接 f"{value}" 会抛 ValueError。
# 这里用 bit_length 估算位数（不真正转字符串，避免触发该限制），超限则降级提示。
_MAX_INT_BITS = 13_000          # ≈ 3913 位十进制，留一点余量


def _int_digits(n: int) -> int:
    """估算正整数 n 的十进制位数（不通过 str(n)，避免 int_max_str_digits 限制）。"""
    if n <= 0:
        return 1
    # log10(2) ≈ 0.30102999566
    return int(n.bit_length() * 0.30102999566) + 1


def calculator(expression: str) -> str:'''
assert old in s, "calculator def anchor missing"
s = s.replace(old, new, 1)

old_ret = '''        return f"{value}"
    except Exception as e:
        return f"计算失败：{e}"'''
new_ret = '''        # 大整数：直接 f-string 会抛 ValueError，先按位数降级
        if isinstance(value, int) and value.bit_length() > _MAX_INT_BITS:
            return (
                f"结果是一个约 {_int_digits(value)} 位的整数，超出可展示范围。"
                f"如需近似值，请说明需要的有效位数。"
            )
        return f"{value}"
    except Exception as e:
        return f"计算失败：{e}"'''
assert old_ret in s, "calculator return anchor missing"
s = s.replace(old_ret, new_ret, 1)

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] calculator.py 大整数降级")

# ============================================================ 2) web_search 配置
p = "app/tool/web_search.py"
s = io.open(p, encoding="utf-8").read()

old_cfg = '''TOOL_CONFIG = {
    # 联网 + 付费额度：超时给足，但配额压低，避免 LLM 循环调用烧钱
    "timeout": 30,
    "retries": 1,
    "quota_per_minute": 20,
}'''
new_cfg = '''TOOL_CONFIG = {
    # 实测 Exa agent 模式单次调用 ~33s，原来的 30s 超时必然触发；
    # 超时后 retries=1 会再吃 30s（总 60s）且最终仍失败 —— 得不偿失。
    # 现在：超时放宽到 60s 让请求能跑完，retries=0 失败即快速降级。
    "timeout": 60,
    "retries": 0,
    "quota_per_minute": 20,
}'''
assert old_cfg in s, "web_search TOOL_CONFIG anchor missing"
s = s.replace(old_cfg, new_cfg, 1)

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] web_search.py timeout/retries 调整")

# ============================================================ 3) aexecute
# 3a) guard 增加 arun（事件循环友好版）
p = "app/tool/guard.py"
s = io.open(p, encoding="utf-8").read()

anchor_run = '''        result = self._invoke_with_retry(meta, kwargs, ctx)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)'''
new_run = '''        result = await self._ainvoke_with_retry(meta, kwargs, ctx)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)'''

if anchor_run in s:
    s = s.replace(anchor_run, new_run, 1)
    print("[ok] guard.arun 接入点替换")
else:
    # run() 是同步方法，这里保留同步实现，另加异步入口
    pass

# 追加 arun + _ainvoke_with_retry（不改动同步路径，避免影响非流式 chat）
addition = '''
    # ------------------------------------------------------------------
    # 异步入口（供 Agent.stream 使用）
    # ------------------------------------------------------------------

    async def arun(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> GuardResult:
        """异步版 run()。

        和 run() 的区别：调用方**不需要**再套一层 asyncio.to_thread ——
        这里直接把同步工具丢进本 guard 的线程池并 await，主事件循环不阻塞，
        且每个工具只占用 1 个线程（run() 被 to_thread 包一层会占 2 个）。
        """
        cfg = meta.config
        started = time.perf_counter()

        if not cfg.enabled:
            return self._blocked("disabled", meta, started)

        user_key = ctx.user_id if ctx.user_id is not None else "__anonymous__"
        if self._quota_exceeded(meta.name, user_key, cfg):
            return self._blocked("quota", meta, started)

        if self._circuit_open(meta.name, cfg):
            return self._blocked("circuit", meta, started)

        result = await self._ainvoke_with_retry(meta, kwargs, ctx)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)

        status = "ok" if result.ok else (result.blocked or "error")
        metrics.inc(M_TOOL_CALLS, 1, tool=meta.name, status=status)
        metrics.observe(M_TOOL_DURATION, result.elapsed_ms, tool=meta.name)

        self._audit(meta, ctx, kwargs, result)
        return result

    # ------------------------------------------------------------------

    async def _ainvoke_with_retry(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> GuardResult:
        """带重试的异步调用。逻辑与 _invoke_with_retry 一致，但等待用 asyncio.sleep。"""
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
                    attempt, attempts, delay, meta.name, last_error[:200],
                )
                await asyncio.sleep(delay)

        return GuardResult(
            ok=False,
            text=f"工具执行失败（已重试{attempts - 1}次）：{last_error}",
            attempts=attempts,
        )

    async def _ainvoke_once(
        self,
        meta: ToolMeta,
        kwargs: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[bool, Any, str]:
        """单次异步调用 + 超时。返回 (ok, value, error)。"""
        loop = asyncio.get_running_loop()
        try:
            if meta.is_async:
                if ctx.loop is None:
                    return False, None, "工具需要异步事件循环，但当前未提供（loop=None）"
                future = asyncio.run_coroutine_threadsafe(
                    self._async_call(meta, kwargs, ctx), ctx.loop,
                )
            else:
                future = loop.run_in_executor(
                    _get_executor(), self._sync_call, meta, kwargs, ctx,
                )
            value = await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=meta.config.timeout,
            )
            return True, value, ""
        except asyncio.TimeoutError as exc:
            return False, None, self._describe(exc, "超时")
        except Exception as exc:                          # noqa: BLE001
            return False, None, self._describe(exc)

'''
marker = "    # ------------------------------------------------------------------\n    # 前置拦截\n"
assert marker in s, "guard marker missing"
s = s.replace(marker, addition + marker, 1)

io.open(p, "w", encoding="utf-8").write(s)
print("[ok] guard.py + arun/_ainvoke_with_retry")
