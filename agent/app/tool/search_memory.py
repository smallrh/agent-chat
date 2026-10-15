"""search_memory 工具：语义检索当前用户的长期记忆。

这是「记忆增强型 Agent」的核心工具——用户问「我上次说的那个项目 deadline 是哪天」
时，Agent 可以调用它从长期记忆库里取回相关事实。

实现要点：
- 这是一个 **async 函数**。由于工具执行跑在 asyncio.to_thread 的 worker 线程里，
  无法直接复用绑定在主事件循环上的 Qdrant / PG / 检索单例；
  执行器会用 run_coroutine_threadsafe 把它桥接回「主事件循环」执行，
  从而安全地复用 memory_retrieval_service 这套已验证的检索链路。
- user_id 由执行器通过 ToolContext 自动注入（LLM 不需要、也不应该知道 user_id）。
- 检索失败一律降级为可读提示，绝不抛异常阻断对话。
"""

from __future__ import annotations


async def search_memory(
    query: str,
    memory_type: str = "",
    user_id: int | None = None,
) -> str:
    """
    按语义检索当前用户的长期记忆。

    Args:
        query:        自然语言检索语句，例如「项目 deadline」「喜欢的食物」。
        memory_type:  可选，限定记忆类型（preference / fact / todo / name）；
                      为空字符串表示检索全部类型。
        user_id:      由执行器自动注入，无需 LLM 提供。

    Returns:
        格式化后的相关记忆文本；无命中或出错时返回可读说明。
    """

    if not query or not query.strip():
        return "检索内容为空。"

    if user_id is None:
        return "缺少用户上下文，无法检索记忆。"

    # 延迟导入：避免模块加载期就强依赖检索链路（embedding / Qdrant / PG）。
    from app.service.retrieval_service import memory_retrieval_service

    try:
        results = await memory_retrieval_service.retrieve(
            user_id=user_id,
            query=query,
            memory_types=[memory_type] if memory_type else None,
            top_k=5,
            record_access=True,
        )
    except Exception as e:
        return f"记忆检索失败：{e}"

    if not results:
        return "未找到相关的长期记忆。"

    lines: list[str] = []
    for i, m in enumerate(results, 1):
        content = (m.get("content") or "").strip()
        mtype = m.get("memory_type") or "unknown"
        score = m.get("score")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "N/A"
        lines.append(f"{i}. [类型:{mtype} | 相关度:{score_str}] {content}")

    return "检索到的相关记忆：\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具注册元数据（registry 自动发现，见 app/tool/spec.py）
# ---------------------------------------------------------------------------

TOOL_SPEC = {
    "description": (
        "按语义检索当前用户的长期记忆（偏好/事实/待办/姓名等），"
        "用于回答与用户个人历史相关的问题，例如"
        "「我上次说的项目 deadline 是哪天」「我喜欢吃什么」。"
    ),
    "parameters": {
        "query": {
            "type": "string",
            "description": "自然语言检索语句，例如「项目 deadline」",
        },
        "memory_type": {
            "type": "string",
            "enum": ["preference", "fact", "todo", "name", ""],
            "description": "可选，限定记忆类型；留空表示全部类型",
        },
    },
    "required": ["query"],
}

TOOL_CONFIG = {
    # 链路含 embedding（LM Studio）+ Qdrant + PG 回表，给宽一点的超时
    "timeout": 30,
    "retries": 1,
    "quota_per_minute": 60,
}
