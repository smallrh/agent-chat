"""web_search 工具：实时联网搜索。

默认提供方为 **Exa Search API**（https://api.exa.ai/search，type=fast），
返回网页标题、链接与摘要。这对应官方 demo 里
`exa.openai.responses.web_search(type="fast")` 的检索能力，
而不是慢且常超时的 `model=agent` Responses 生成。

Tavily 作为可切换的兜底提供方（WEB_SEARCH_PROVIDER="tavily"）。
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings
from app.tool.context import get_tool_context
from app.tool import search_cache

logger = logging.getLogger(__name__)

_EXA_SEARCH_ENDPOINT = "https://api.exa.ai/search"
_EXA_TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=5.0)


def web_search(query: str, top_k: int = 5) -> str:
    """
    实时联网搜索，返回与问题相关的网页标题、链接与摘要。

    Args:
        query: 搜索关键词。
        top_k: 返回条数，默认 5。

    Returns:
        格式化后的搜索结果；未配置 Key 或出错时返回可读说明。
    """
    ctx = get_tool_context()
    cached = search_cache.get_with_meta(ctx.conversation_id, query, top_k)
    if cached:
        text, kind = cached
        logger.info("web_search cache hit conv=%s kind=%s", ctx.conversation_id, kind)
        note = (
            "（同一会话内复用了刚才的搜索结果，未再次请求网络。）"
            if kind == "exact"
            else "（同一会话内已有高度相似的搜索结果，已直接复用、未再次请求网络。"
            "若确实需要不同角度的资料，请换一个差异明显的关键词。）"
        )
        return text + "\n\n" + note

    provider = (settings.WEB_SEARCH_PROVIDER or "exa").lower()

    try:
        if provider == "exa":
            text = _exa_search(query, top_k)
        elif provider == "tavily":
            api_key = settings.WEB_SEARCH_API_KEY
            if not api_key:
                return (
                    "未配置 Tavily API Key（请在 .env 设置 WEB_SEARCH_API_KEY，"
                    "或将 WEB_SEARCH_PROVIDER 设为 exa）。"
                )
            text = _tavily_search(query, top_k, api_key)
        else:
            return f"不支持的搜索提供方：{provider}（当前支持 exa / tavily）"
        if text and not text.startswith("[web_search]") and "失败" not in text[:40]:
            search_cache.put(ctx.conversation_id, query, top_k, text)
        return text
    except Exception as e:
        logger.exception("web_search failed (provider=%s)", provider)
        return f"联网搜索失败：{e}"


def _exa_search(query: str, top_k: int = 5) -> str:
    """调用 Exa /search（fast），不走 model=agent 的长生成。"""
    api_key = settings.EXA_API_KEY or settings.WEB_SEARCH_API_KEY
    if not api_key:
        return (
            "未配置 Exa API Key（请在 .env 设置 EXA_API_KEY，"
            "或将 WEB_SEARCH_PROVIDER 设为 tavily）。"
        )

    n = max(1, min(int(top_k or 5), 10))
    payload = {
        "query": query,
        "type": "auto",
        "numResults": n,
        "useAutoprompt": True,
        "contents": {
            "text": {"maxCharacters": 2000},
            "highlights": {"maxCharacters": 400},
        },
    }

    try:
        resp = httpx.post(
            _EXA_SEARCH_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            json=payload,
            timeout=_EXA_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException as e:
        logger.warning("Exa search timeout: %s", e)
        return "[web_search] Exa 检索超时，请稍后重试或换一个更具体的关键词。"
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        logger.warning("Exa search HTTP %s: %s", e.response.status_code, body)
        return f"[web_search] Exa 检索失败（HTTP {e.response.status_code}）。"
    except Exception as e:
        logger.exception("Exa search failed")
        return f"[web_search] Exa 检索失败：{e}"

    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        return f"[web_search] Exa 未检索到相关结果（query={query!r}）。"

    lines: list[str] = [f"联网搜索结果（query={query}）："]
    for i, r in enumerate(results[:n], 1):
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "(无标题)").strip()
        url = (r.get("url") or "").strip()
        highlights = r.get("highlights") or []
        snippet = ""
        if isinstance(highlights, list) and highlights:
            snippet = " ".join(str(h) for h in highlights[:2])
        if not snippet:
            text = r.get("text") or r.get("summary") or ""
            snippet = str(text).replace("\n", " ").strip()[:800]
        date = (r.get("publishedDate") or r.get("published_date") or "").strip()
        date_suffix = f" ({date})" if date else ""
        lines.append(f"{i}. {title}{date_suffix}")
        if url:
            lines.append(f"   来源: {url}")
        if snippet:
            lines.append(f"   摘要: {snippet[:800]}")

    return "\n".join(lines).strip()


def _tavily_search(query: str, top_k: int = 5, api_key: str = "") -> str:
    """Tavily 兜底搜索（纯 httpx 同步调用）。"""
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": int(top_k),
            "search_depth": "basic",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results") or []
    if not results:
        return "未检索到相关网页结果。"

    lines: list[str] = []
    for i, r in enumerate(results[: int(top_k)], 1):
        title = r.get("title", "")
        url = r.get("url", "")
        content = (r.get("content") or "").strip().replace("\n", " ")
        lines.append(f"{i}. {title}\n   {url}\n   {content[:300]}")

    return "联网搜索结果：\n" + "\n\n".join(lines)


TOOL_SPEC = {
    "description": (
        "实时联网搜索。返回若干条「标题 + URL + 摘要」，这是回答的唯一事实来源。"
        "用于地点、酒店、公司、新闻、价格等需要核实的问题。"
        "同一用户问题通常调用一次即可；已有标题和 URL 时不要为了「再精确一点」立刻重搜。"
        "调用后必须按返回的标题/链接组织答案并引用 URL；"
        "不要把摘要里没有的地址、评分、地铁站写进去；"
        "若结果对应多家店或主体不清，列出候选并请用户确认。"
    ),
    "parameters": {
        "query": {
            "type": "string",
            "description": "搜索关键词",
        },
        "top_k": {
            "type": "integer",
            "description": "返回条数，默认5",
        },
    },
    "required": ["query"],
}

TOOL_CONFIG = {
    "timeout": 18,
    "retries": 0,
    "quota_per_minute": 20,
}
