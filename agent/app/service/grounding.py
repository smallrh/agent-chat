"""事实 grounding：让答案里的引用可回溯到工具原文，并在检索无果时禁止编造。

背景（对应《现状和不足.md》Agent 缺点 1）：
`web_search` 只给标题+摘要，模型容易二次搜索或脑补地址/评分。
原先只靠 system prompt 约束（软约束），这里补一层**硬校验**：

  1. 解析 web_search 文本 → 结构化来源（title / url / snippet）
  2. 判定本次检索结果状态：ok（有条目）/ empty（无条目）/ failed（报错或超时）
  3. 校验最终答案里的 URL 是否都来自工具原文（不来自原文 = 可疑引用）
  4. 检索 empty/failed 时：给模型注入「禁止编造」提醒 + 给答案追加「未核实」说明

所有行为都受 settings.GROUNDING_* 开关控制，可一键回退到旧行为。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s\]）)）>\"']+", re.I)
_ITEM_RE = re.compile(r"^\d+[\.、)）]\s+(.+)$")

# web_search 明确「失败 / 未配置 / 无结果」的特征串（覆盖 exa 与 tavily 两条实现）
_FAILURE_MARKERS = (
    "[web_search]",
    "联网搜索失败",
    "未配置",
    "不支持的搜索提供方",
)
_EMPTY_MARKERS = (
    "未检索到",
    "未检索到相关网页结果",
)

_SEARCH_TOOL_NAMES = frozenset({"web_search"})

# 检索无可用结果时，注入给模型的强约束提醒（比 system prompt 更靠近证据，效果更稳）
NO_RESULT_REMINDER = (
    "[系统校验] 上一次 web_search 没有返回可用的检索结果（失败 / 超时 / 无条目）。"
    "你必须明确告诉用户「联网检索未获得结果，无法核实」，"
    "禁止给出任何具体事实：地址、电话、评分、价格、营业时间、地铁站、周边商户。"
    "可以基于常识给一般性建议，但要说明这不是检索所得、需要用户自行核实。"
)

# 检索无可用结果时，追加在答案末尾的降级说明（给用户看的）
UNVERIFIED_NOTE = (
    "\n\n---\n⚠️ 本次联网检索未返回可用结果，以上内容未经外部来源核实，"
    "请谨慎参考；建议稍后重试或提供更具体的关键词。"
)

_SOURCE_BLOCK_HEADER = (
    "\n\n---\n检索来源（请以链接原文为准；摘要未出现的地址/评分未写入正文）：\n"
)


@dataclass(frozen=True)
class SearchOutcome:
    """一次（或多轮）联网检索的整体结果判定。"""

    status: str  # "none" | "ok" | "empty" | "failed"
    reason: str = ""

    @property
    def has_evidence(self) -> bool:
        return self.status == "ok"

    @property
    def attempted_but_failed(self) -> bool:
        return self.status in ("empty", "failed")


def _clean_url(url: str) -> str:
    return (url or "").rstrip(".,;，。）)】")


def is_search_tool(name: str | None) -> bool:
    return (name or "") in _SEARCH_TOOL_NAMES


# ------------------------------------------------------------------
# 1. 解析：web_search 文本 → 结构化来源
# ------------------------------------------------------------------

def parse_sources(result_text: str) -> list[dict]:
    """从 web_search 的返回文本里解析出结构化来源条目。

    同时兼容两种输出格式：
      - Exa： 「1. 标题 (日期)」 / 「   来源: URL」 / 「   摘要: ...」
      - Tavily：「1. 标题」 / 「   URL」 / 「   正文...」

    只保留能解析出 URL 的条目（没有 URL 的来源无法被用户核实）。
    """
    sources: list[dict] = []
    cur: dict | None = None

    for raw_line in (result_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 新条目：行首是「1. 」「2、」这类序号
        m = _ITEM_RE.match(line)
        if m and not line.lower().startswith("http"):
            if cur:
                sources.append(cur)
            cur = {"title": m.group(1).strip(), "url": "", "snippet": ""}
            continue

        if cur is None:
            continue

        # URL
        mu = _URL_RE.search(line)
        if mu:
            cur["url"] = _clean_url(mu.group(0))
            continue

        # 摘要（中英文冒号都认）
        if line.startswith(("摘要:", "摘要：", "snippet:", "Snippet:")):
            cur["snippet"] = re.split(r"[:：]", line, maxsplit=1)[-1].strip()
            continue

        # 其余行当作正文片段（Tavily 的 content 是裸行）
        if not cur["snippet"]:
            cur["snippet"] = line[:300]

    if cur:
        sources.append(cur)

    return [s for s in sources if s["url"]]


def collect_sources(tool_traces: list[dict]) -> list[dict]:
    """汇总一轮对话里全部 web_search 的来源（按出现顺序去重，最多 8 条）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for t in tool_traces or []:
        if not is_search_tool(t.get("name")):
            continue
        for s in parse_sources(t.get("result") or ""):
            url = s["url"]
            if url in seen:
                continue
            seen.add(url)
            out.append(s)
            if len(out) >= 8:
                return out
    return out


def extract_urls(text: str) -> list[str]:
    """从任意文本里抽出 URL（用于校验正文引用）。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _URL_RE.finditer(text or ""):
        u = _clean_url(m.group(0))
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ------------------------------------------------------------------
# 2. 判定：这次检索到底成没成
# ------------------------------------------------------------------

def classify_single(tool_name: str | None, result: str | None) -> str:
    """判定**单次** web_search 的结果状态：ok / empty / failed / none。"""
    if not is_search_tool(tool_name):
        return "none"
    text = (result or "").strip()
    if not text:
        return "failed"
    if any(mk in text for mk in _EMPTY_MARKERS):
        return "empty"
    if any(mk in text for mk in _FAILURE_MARKERS):
        return "failed"
    return "ok" if parse_sources(text) else "empty"


def classify(tool_traces: list[dict]) -> SearchOutcome:
    """判定**整轮**联网检索的结果状态。

    只要有一次成功返回条目 → ok（证据最优先）；
    否则区分「报错」与「空结果」，供上层决定降级文案。
    """
    search_traces = [t for t in (tool_traces or []) if is_search_tool(t.get("name"))]
    if not search_traces:
        return SearchOutcome("none", "本轮没有调用 web_search")

    statuses = [
        classify_single(t.get("name"), t.get("result")) for t in search_traces
    ]
    if "ok" in statuses:
        return SearchOutcome("ok", f"{statuses.count('ok')} 次检索拿到条目")

    if "failed" in statuses:
        return SearchOutcome("failed", "web_search 报错/超时/未配置 Key")

    return SearchOutcome("empty", "web_search 返回空结果")


# ------------------------------------------------------------------
# 3. 校验：正文里的引用是否都能在工具原文里找到
# ------------------------------------------------------------------

def unknown_urls(content: str, tool_traces: list[dict]) -> list[str]:
    """正文里出现、但工具原文里没有的 URL（可疑引用 / 可能是模型脑补）。"""
    if not content:
        return []
    allowed = {s["url"] for s in collect_sources(tool_traces)}
    return [u for u in extract_urls(content) if u not in allowed]


def _strip_urls(content: str, urls: list[str]) -> str:
    """把指定 URL 从正文里剔除（GROUNDING_STRIP_UNKNOWN_URLS=True 时用）。"""
    text = content or ""
    for u in urls:
        text = text.replace(u, "")
    return text


def append_sources(content: str, tool_traces: list[dict]) -> str:
    """正文一个来源都没引用时，把检索来源补到答案末尾。"""
    sources = collect_sources(tool_traces)
    if not sources:
        return content
    if any(s["url"] in (content or "") for s in sources):
        return content
    lines = "".join(f"- {s['url']}\n" for s in sources)
    return (content or "").rstrip() + _SOURCE_BLOCK_HEADER + lines


# ------------------------------------------------------------------
# 4. 总入口：对最终答案执行 grounding
# ------------------------------------------------------------------

def enforce(content: str, tool_traces: list[dict]) -> str:
    """对最终答案做一次 grounding 收尾，返回处理后的正文。

    - 检索成功：可疑 URL 告警（可选剔除）；一个引用都没有 → 补来源块
    - 检索失败/空结果：追加「未核实」降级说明
    - 没调用过搜索：原样返回
    """
    if not settings.GROUNDING_ENABLED:
        return content

    outcome = classify(tool_traces)
    text = content or ""

    if outcome.has_evidence:
        unknown = unknown_urls(text, tool_traces)
        if unknown:
            logger.warning(
                "grounding: answer cites %d URL(s) not found in tool output: %s",
                len(unknown), unknown[:3],
            )
            if settings.GROUNDING_STRIP_UNKNOWN_URLS:
                text = _strip_urls(text, unknown)
        return append_sources(text, tool_traces)

    if outcome.attempted_but_failed and settings.GROUNDING_APPEND_FAILURE_NOTE:
        logger.info(
            "grounding: search %s (%s) → append unverified note",
            outcome.status, outcome.reason,
        )
        return text.rstrip() + UNVERIFIED_NOTE

    return text


def reminder_for(tool_name: str | None, result: str | None) -> str | None:
    """工具结果「无可用证据」时，返回要注入给模型的提醒；否则返回 None。"""
    if not settings.GROUNDING_ENABLED or not settings.GROUNDING_INJECT_REMINDER:
        return None
    if classify_single(tool_name, result) in ("empty", "failed"):
        return NO_RESULT_REMINDER
    return None
