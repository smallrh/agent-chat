"""后台任务 worker（P2 真任务队列）。

V009 建的 tasks 表 + TaskService 只有 `demo` 一个假 handler（纯 sleep），
《现状和不足.md》里把这条列为「任务系统偏 demo：没有真正的调研/长跑 worker」。
本文件补上真实 worker 与执行契约。

## 三个核心设计

### 1. handler 契约
每个 handler 都是 `async def`，签名统一：

    async def handler(*, task_id, progress, cancel_token,
                      conversation_id, user_id, goal, title) -> dict

- `progress(done, total, message)` —— 上报进度。message 会落库成 step，
  前端进度条下面显示的就是它（「正在检索：广州天河酒店 2/3」）。
- `cancel_token` —— 协作式取消令牌。handler 必须在每个子步前后
  `cancel_token.raise_if_cancelled()`，否则用户点「取消」后任务会继续跑完
  （asyncio.Task.cancel 只能在 await 点生效，而同步 HTTP 调用没有 await 点）。
- 返回 dict 会原样进 `tasks.result`，并由 TaskService 格式化成一条
  assistant 消息写回会话。

### 2. 协作式取消（为什么不能只靠 asyncio 取消）
`TaskService.cancel_task()` 会调 `asyncio.Task.cancel()`，但 research
handler 里真正的耗时是 `web_search` 的同步 httpx 调用 —— 它在
`asyncio.to_thread` 里跑，线程无法被 asyncio 取消打断。因此必须：
  ① 置 token（handler 在检查点主动抛 `TaskCancelled`）
  ② `Task.cancel()` 兜底（handler 恰好卡在 await 上时立即生效）
两条路径都会走到 `_run` 的终态写入，不会留下「永远 running」的脏行。

### 3. 失败降级
调研任务依赖外部 LLM + 搜索 API，任一环节失败都不应该让整个任务 failed：
  - 拆子查询失败 → 退化成「直接用 goal 原样搜一次」
  - 某个子查询失败 → 跳过它，其余结果照常汇总
  - 汇总 LLM 失败 → 退化成把检索原文拼成「资料汇总」，不让用户空手而归
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable

from app.core.config import settings
from app.service.grounding import parse_sources


logger = logging.getLogger(__name__)


# =====================================================================
# 取消
# =====================================================================


class TaskCancelled(Exception):
    """worker 在检查点主动抛出的「已取消」信号。

    与 asyncio.CancelledError 区分开：后者是 Task.cancel() 打进来的，
    前者是 handler 自己发现 token 被置位抛的。TaskService 对两者
    一视同仁（都落成 cancelled 终态）。
    """


class TaskCancelToken:
    """协作式取消令牌。

    只用一个 asyncio.Event，避免引入额外锁。handler 侧用法：

        cancel_token.raise_if_cancelled()          # 检查点：已取消就抛
        await cancel_token.sleep(1.0)              # 可中断的 sleep
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TaskCancelled("task cancelled by user")

    async def sleep(self, seconds: float) -> None:
        """可中断的 sleep：取消后立刻返回，而不是白等一轮。"""
        if self._event.is_set():
            raise TaskCancelled("task cancelled by user")
        try:
            await asyncio.wait_for(self._event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise TaskCancelled("task cancelled by user")


# handler 类型
ProgressFn = Callable[[int, int, str], Awaitable[None]]
HandlerFn = Callable[..., Awaitable[dict]]


# =====================================================================
# LLM 助手（worker 内部共用）
# =====================================================================


def _llm_text(prompt: str, system: str, timeout: float | None = None) -> str:
    """同步调一次 LLM 并取回文本。必须放在 to_thread 里调用。"""
    from app.agent.llm import llm

    resp = llm.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        stream=False,
        timeout=timeout,
    )
    choices = getattr(resp, "choices", None) or []
    if not choices:
        raise RuntimeError("LLM 返回空 choices")
    return (choices[0].message.content or "").strip()


async def _llm_text_async(prompt: str, system: str, timeout: float | None = None) -> str:
    """把同步 LLM 调用丢到线程，避免堵住事件循环。"""
    return await asyncio.to_thread(_llm_text, prompt, system, timeout)


# =====================================================================
# kind=demo（内置，无外部依赖，端到端自检用）
# =====================================================================


async def demo_handler(
    *,
    task_id: str,
    progress: ProgressFn,
    cancel_token: TaskCancelToken,
    conversation_id: str,
    user_id: int,
    goal: str,
    title: str,
) -> dict:
    """模拟多步长任务：每步 sleep 一下并上报进度 + 步骤名。"""
    steps = [
        "理解需求并拆解子任务",
        "检索相关资料",
        "分析与整合信息",
        "生成草稿",
        "校对与输出",
    ]
    for i, step in enumerate(steps, 1):
        cancel_token.raise_if_cancelled()
        await cancel_token.sleep(0.35)
        cancel_token.raise_if_cancelled()
        await progress(i, len(steps), step)

    return {
        "summary": f"已完成对「{goal or title}」的处理",
        "steps": steps,
        "goal": goal,
    }


# =====================================================================
# kind=research（真实调研 worker）
# =====================================================================

_PLAN_SYSTEM = (
    "你是一个调研规划助手。用户会给你一个调研目标，你要把它拆成若干个"
    "彼此不重叠、可直接用于联网搜索的中文查询词。"
    "只输出一个 JSON 数组（字符串数组），不要任何解释、不要 Markdown 代码块。"
)

_REPORT_SYSTEM = (
    "你是一个严谨的调研分析助手。你只会收到【联网检索到的原始资料】，"
    "必须只依据这些资料写中文报告。\n"
    "硬约束：\n"
    "1. 不得编造资料里没有出现的具体事实（地址、电话、价格、评分、时间等）。\n"
    "2. 报告末尾用 Markdown 列出【参考来源】小节，每条一行："
    "- [标题](URL)\n"
    "3. 若资料不足以回答，直接说明「检索到的资料不足以确认」，不要硬凑。\n"
    "4. 用 Markdown 写，结构清晰，控制在合理长度。"
)


def _parse_queries(text: str, fallback: str, max_n: int) -> list[str]:
    """从 LLM 输出里稳健地解析出查询词数组。

    LLM 经常无视「只要 JSON」的指令（外面套 ```json、前后加解释），
    这里做三档容错：整体 JSON → 截取 [...] 片段 → 逐行兜底。
    """
    raw = (text or "").strip()
    candidates: list[str] = []

    # 1) 直接解析
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            candidates = [str(x).strip() for x in data if str(x).strip()]
    except (ValueError, TypeError):
        pass

    # 2) 截取第一个 JSON 数组片段
    if not candidates:
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, list):
                    candidates = [str(x).strip() for x in data if str(x).strip()]
            except (ValueError, TypeError):
                pass

    # 3) 逐行兜底（去掉序号、引号、Markdown 列表符号）
    if not candidates:
        for line in raw.splitlines():
            line = re.sub(r"^\s*[-*•\d]+[.、)]?\s*", "", line).strip().strip('"\'`,，。 ')
            if 2 <= len(line) <= 60:
                candidates.append(line)

    # 去重 + 截断
    seen: set[str] = set()
    out: list[str] = []
    for q in candidates:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
        if len(out) >= max_n:
            break

    if not out:
        out = [fallback[:120]] if fallback else []
    return out


async def research_handler(
    *,
    task_id: str,
    progress: ProgressFn,
    cancel_token: TaskCancelToken,
    conversation_id: str,
    user_id: int,
    goal: str,
    title: str,
) -> dict:
    """
    真实调研 worker：拆子查询 → 逐个联网检索 → LLM 汇总成带来源的报告。

    步骤设计（总步数 = 1 规划 + N 检索 + 1 汇总）：
      1/N  规划检索词
      2..  逐个检索（每完成一个上报一次）
      最后 汇总成报告
    """
    from app.tool.context import ToolContext, tool_context_scope
    from app.tool.web_search import web_search

    subject = (goal or title or "").strip()
    if not subject:
        raise ValueError("调研任务缺少目标（goal / title 均为空）")

    max_queries = max(1, int(settings.TASK_RESEARCH_MAX_QUERIES))
    top_k = max(1, int(settings.TASK_RESEARCH_TOP_K))

    # ---------- 步骤 1：规划检索词 ----------
    # 总步数先按「1 规划 + max_queries 检索 + 1 汇总」报，
    # 实际 query 数可能少于 max_queries，后面用 steps_total 动态校正。
    total_steps = 1 + max_queries + 1
    cancel_token.raise_if_cancelled()
    await progress(1, total_steps, "拆解调研子问题")

    queries: list[str] = []
    try:
        plan_raw = await _llm_text_async(
            f"调研目标：{subject}\n\n请拆成最多 {max_queries} 个检索词，输出 JSON 数组。",
            _PLAN_SYSTEM,
            timeout=float(settings.TASK_RESEARCH_LLM_TIMEOUT),
        )
        queries = _parse_queries(plan_raw, subject, max_queries)
    except Exception:
        logger.exception("research: planning failed, fallback to raw subject")
        queries = [subject[:120]]

    cancel_token.raise_if_cancelled()
    actual_total = 1 + len(queries) + 1
    await progress(1, actual_total, f"已拆解 {len(queries)} 个检索方向")

    # ---------- 步骤 2：逐个检索 ----------
    # web_search 是同步函数且依赖 ToolContext（会话级搜索缓存），
    # 这里补上 context 再丢线程池，保证缓存命中与对话链路一致。
    ctx = ToolContext(user_id=user_id, conversation_id=conversation_id)
    collected: list[dict] = []
    failed: list[str] = []

    for i, q in enumerate(queries, start=1):
        cancel_token.raise_if_cancelled()

        def _do_search(query: str = q) -> str:
            with tool_context_scope(ctx):
                return web_search(query, top_k)

        text = await asyncio.to_thread(_do_search)
        cancel_token.raise_if_cancelled()

        sources = parse_sources(text or "")
        low = (text or "").lower()
        usable = bool(sources) and "失败" not in (text or "")[:40]
        if usable:
            collected.append({"query": q, "text": text, "sources": sources})
        else:
            failed.append(q)
            logger.warning("research: query yielded nothing: %s", q)
        del low

        await progress(1 + i, actual_total, f"检索资料（{i}/{len(queries)}）：{q[:24]}")

    if not collected:
        # 一条都没搜到：不编造，明确告诉用户检索失败。
        return {
            "kind": "research",
            "summary": "本次联网检索没有返回可用结果，未能完成调研。",
            "steps": [f"检索：{q}" for q in queries],
            "queries": queries,
            "failed_queries": failed or queries,
            "sources": [],
            "report": (
                f"## 调研未完成：{title}\n\n"
                f"目标：{subject}\n\n"
                "⚠️ 联网检索未返回可用结果（可能是检索服务未配置 Key、"
                "被限流或网络不可达），因此**不提供未经核实的结论**。"
                "请稍后重试，或补充更具体的检索词。"
            ),
            "degraded": True,
        }

    # ---------- 步骤 3：汇总成报告 ----------
    cancel_token.raise_if_cancelled()
    await progress(actual_total, actual_total, "汇总并生成报告")

    evidence_parts = []
    for idx, item in enumerate(collected, 1):
        evidence_parts.append(
            f"### 资料 {idx}（检索词：{item['query']}）\n{item['text']}"
        )
    evidence = "\n\n".join(evidence_parts)
    # 上下文保护：单次注入的资料长度封顶，避免把 LLM 上下文撑爆
    if len(evidence) > settings.TASK_RESEARCH_MAX_EVIDENCE_CHARS:
        evidence = evidence[: settings.TASK_RESEARCH_MAX_EVIDENCE_CHARS] + "\n…（资料过长已截断）"

    prompt = (
        f"调研目标：{subject}\n\n"
        f"标题：{title}\n\n"
        f"以下是联网检索到的原始资料：\n\n{evidence}\n\n"
        "请据此写一份中文调研报告。"
    )

    report = ""
    try:
        report = await _llm_text_async(
            prompt,
            _REPORT_SYSTEM,
            timeout=float(settings.TASK_RESEARCH_LLM_TIMEOUT),
        )
    except Exception:
        logger.exception("research: report generation failed, degrade to raw digest")
        report = ""

    if not report:
        # 汇总失败也不能让用户空手：退化成结构化资料摘要（仍然带来源）
        lines = [f"## 调研资料汇总：{title}", "", f"目标：{subject}", ""]
        for idx, item in enumerate(collected, 1):
            lines.append(f"### {idx}. {item['query']}")
            for s in item["sources"][:5]:
                s_title = (s.get("title") or "来源").strip()
                s_url = (s.get("url") or "").strip()
                lines.append(f"- [{s_title}]({s_url})" if s_url else f"- {s_title}")
            snippet = (item["sources"][0].get("snippet") or "").strip() if item["sources"] else ""
            if snippet:
                lines.append(f"  > {snippet[:200]}")
            lines.append("")
        lines.append(
            "⚠️ 自动汇总环节失败，以上为检索原文摘要，请自行判断。"
        )
        report = "\n".join(lines)

    all_sources: list[dict] = []
    seen_urls: set[str] = set()
    for item in collected:
        for s in item["sources"]:
            url = (s.get("url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_sources.append(s)

    cancel_token.raise_if_cancelled()
    return {
        "kind": "research",
        "summary": _first_paragraph(report) or f"已完成「{title}」的调研",
        "steps": [f"拆解：{len(queries)} 个方向"]
        + [f"检索：{q}" for q in queries]
        + ["汇总报告"],
        "queries": queries,
        "failed_queries": failed,
        "sources": all_sources[:20],
        "report": report,
        "degraded": False,
    }


def _first_paragraph(md: str) -> str:
    """取 Markdown 第一段非空文本作为一句话摘要。"""
    for line in (md or "").splitlines():
        t = line.strip().lstrip("#").strip()
        if len(t) >= 10:
            return t[:200]
    return ""


# =====================================================================
# 注册
# =====================================================================


def register_default_handlers(service: Any) -> None:
    """把所有内置 handler 注册到 TaskService。

    抽成函数而不是在 TaskService 里硬编码，是为了让「新增一种任务类型」
    只改本文件一处 —— TaskService 只认 kind 字符串。
    """
    service.register_handler("demo", demo_handler)
    service.register_handler("research", research_handler)
