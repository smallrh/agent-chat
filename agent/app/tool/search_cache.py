"""同一会话内短窗口 web_search 去重（精确命中 + 相似命中）。

两档命中：
  - exact  ：归一化后完全相同的 query（原行为）
  - similar：query 变了但语义高度重合（如「广州天河酒店」→「广州天河区酒店推荐」），
             这类「换个近义词再搜一遍」是模型最常见的浪费调用，直接复用上次结果。

相似度算法刻意做得**轻**：中文按 bigram、英文/数字按词切 token，
用「交集 / 较小集合」作分母——这样**更具体的 query 能命中更宽泛的 query**
（「天河区酒店」命中「广州酒店」），符合"已经有结果就别再搜"的目标。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time

from app.core.config import settings

_TTL_SECONDS = 180.0
_lock = threading.Lock()
# key: (conversation_id, digest) → (过期时间戳, 结果文本, 归一化 query, top_k)
_CACHE: dict[tuple[str, str], tuple[float, str, str, int]] = {}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _norm_query(query: str) -> str:
    return " ".join((query or "").strip().lower().split())


def _tokens(query: str) -> set[str]:
    """把 query 切成 token 集合：中文 bigram + 英文/数字词。"""
    q = _norm_query(query)
    toks: set[str] = set(_WORD_RE.findall(q))
    for seg in _CJK_RE.findall(q):
        if len(seg) == 1:
            toks.add(seg)
        else:
            toks.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return toks


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def cache_key(conversation_id: str | None, query: str, top_k: int) -> tuple[str, str] | None:
    if not conversation_id:
        return None
    q = _norm_query(query)
    if not q:
        return None
    digest = hashlib.sha256(f"{q}|{int(top_k or 5)}".encode("utf-8")).hexdigest()[:24]
    return (conversation_id, digest)


def _not_expired(hit: tuple[float, str, str, int], now: float) -> bool:
    return now - hit[0] <= _TTL_SECONDS


def get(conversation_id: str | None, query: str, top_k: int) -> str | None:
    """精确命中（旧行为保留）。命中返回结果文本，否则 None。"""
    hit = get_with_meta(conversation_id, query, top_k)
    if hit and hit[1] == "exact":
        return hit[0]
    return None


def get_with_meta(
    conversation_id: str | None, query: str, top_k: int
) -> tuple[str, str] | None:
    """去重查询，返回 (结果文本, 命中类型)。

    命中类型：`exact`（同一 query）或 `similar`（高相似度 query）；未命中返回 None。
    """
    if not conversation_id:
        return None

    now = time.monotonic()
    want_k = int(top_k or 5)

    with _lock:
        # ---- 1) 精确命中 ----
        key = cache_key(conversation_id, query, want_k)
        if key is not None:
            hit = _CACHE.get(key)
            if hit and _not_expired(hit, now):
                return hit[1], "exact"

        if not settings.SEARCH_DEDUP_SIMILAR_ENABLED:
            return None

        # ---- 2) 相似命中 ----
        # 只复用「条数不少于本次请求」的结果，避免用 3 条的旧结果顶替要 8 条的新请求。
        q_tokens = _tokens(query)
        if not q_tokens:
            return None

        best: tuple[float, str] | None = None
        for (conv, _digest), (ts, text, cached_q, cached_k) in _CACHE.items():
            if conv != conversation_id or ts < now - _TTL_SECONDS:
                continue
            # 只复用「条数不少于本次请求」的结果，避免用 3 条的旧结果顶替要 8 条的新请求
            if cached_k < want_k:
                continue
            score = _similarity(q_tokens, _tokens(cached_q))
            if score >= settings.SEARCH_DEDUP_SIMILARITY:
                if best is None or score > best[0]:
                    best = (score, text)

        if best is None:
            return None
        return best[1], "similar"


def put(conversation_id: str | None, query: str, top_k: int, text: str) -> None:
    key = cache_key(conversation_id, query, top_k)
    if key is None or not text:
        return
    with _lock:
        if len(_CACHE) > 500:
            cutoff = time.monotonic() - _TTL_SECONDS
            stale = [k for k, (ts, _, _, _) in _CACHE.items() if ts < cutoff]
            for k in stale:
                _CACHE.pop(k, None)
        _CACHE[key] = (time.monotonic(), text, _norm_query(query), int(top_k or 5))
