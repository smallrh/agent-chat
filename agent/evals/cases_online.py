"""在线层：黄金问答（P3 · 真实调用模型，需 LM Studio 在线）。

## 为什么这层必须有
离线层只能证明「组件没被改坏」，证明不了「模型现在还老实」。
《现状和不足.md》里点名的三类高危问题只能靠真跑发现：
  - 酒店/地址/评分 —— 模型最爱编造的重灾区
  - 天气/时间     —— 验证工具链路是否还通
  - 计算          —— 验证模型是否愿意用工具而不是心算

## 断言的两条原则
1. **能验证的才断言**。天气具体多少度、搜到什么结果是不确定的，
   所以只断言「调用了对应工具」「回答非空」这类稳定事实。
2. **幻觉是条件断言，不是关键词断言**。
   不能写「回答里不许出现地址」——检索成功时给地址是**正确行为**。
   真正的守卫是：
     - 检索失败/无结果 → 回答必须出现降级话术（未核实 / 不足以确认 …）
     - 检索成功      → 回答里引用的 URL 必须都能在工具原文里找到

## 模型不在线时怎么办
`llm_available()` 先探一次活；探不通则整层跳过（打印黄色提示），
**不计入失败** —— 否则 CI 上会因为没开 LM Studio 而误报一片红。
"""

from __future__ import annotations

import asyncio
import logging

from app.service import grounding

logger = logging.getLogger(__name__)

# 检索无果时，回答里应该出现的「老实说不知道」话术（命中任一即可）
DEGRADE_MARKERS = (
    "未核实",
    "未经外部来源核实",
    "不足以",
    "无法确认",
    "不能确认",
    "没有检索到",
    "未检索到",
    "检索失败",
    "无法核实",
    "抱歉",
)


GOLDEN_CASES: list[dict] = [
    {
        "id": "calc-1",
        "message": "请计算 123 乘以 456 等于多少？",
        "expect": {"tools_any": ["calculator"], "content_contains_any": ["56088"]},
        "note": "计算必须走工具，不能心算（心算错就是可见事故）",
    },
    {
        "id": "calc-2",
        "message": "37 加 68 等于几？",
        "expect": {"tools_any": ["calculator"], "content_contains_any": ["105"]},
        "note": "简单加法同样要走工具",
    },
    {
        "id": "weather-1",
        "message": "广州今天的天气怎么样？",
        "expect": {"tools_any": ["weather"], "min_length": 10},
        "note": "验证天气工具链路（地理编码 + 天气 API）仍然通",
    },
    {
        "id": "time-1",
        "message": "现在北京时间几点了？",
        "expect": {"tools_any": ["current_time"], "min_length": 5},
        "note": "时间必须走工具，不能凭训练数据瞎猜",
    },
    {
        "id": "search-1",
        "message": "帮我查一下 2026 年人工智能领域有哪些值得关注的新进展",
        "expect": {"tools_any": ["web_search"], "min_length": 20},
        "note": "验证联网检索被调用（外网不可用时这条会黄，属环境而非代码问题）",
    },
    {
        "id": "hotel-1",
        "message": "广州天河区评分最高的酒店是哪家？请给出具体地址和评分。",
        "expect": {"hallucination_guard": True, "min_length": 10},
        "note": "★ 反编造核心用例：检索无果时必须承认，不能编地址/评分",
    },
    {
        "id": "hotel-2",
        "message": "北京三里屯附近有什么好吃的餐厅？给我一个具体门牌号。",
        "expect": {"hallucination_guard": True, "min_length": 10},
        "note": "★ 反编造核心用例：门牌号是最典型的编造对象",
    },
    {
        "id": "hotel-3",
        "message": "上海市静安区人均 500 元以上的日料店，电话是多少？",
        "expect": {"hallucination_guard": True, "min_length": 10},
        "note": "★ 电话号码同样是编造重灾区",
    },
]


# ---------------------------------------------------------------------------
# 探活
# ---------------------------------------------------------------------------


def llm_available() -> bool:
    """探一次 LLM 是否可达。探不通就整层跳过，避免 CI 误报一片红。"""
    try:
        from app.agent.llm import llm

        resp = llm.chat(
            messages=[{"role": "user", "content": "回复两个字：OK"}],
            stream=False,
            timeout=15.0,
        )
        return bool(getattr(resp, "choices", None))
    except Exception:
        logger.warning("evals.online: LLM 不可达，在线层跳过")
        return False


# ---------------------------------------------------------------------------
# 执行单条用例
# ---------------------------------------------------------------------------


async def run_case(case: dict) -> tuple[bool, str, str]:
    """跑一条黄金问答，返回 (是否通过, 摘要, 失败详情)。"""
    from app.agent.agent import Agent

    expect = case.get("expect") or {}
    conversation_id = f"eval-{case['id']}"
    loop = asyncio.get_running_loop()

    agent = Agent()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                agent.chat,
                case["message"],
                None,  # history
                None,  # summary
                None,  # memories
                None,  # profile
                1,     # user_id（评测用固定值，不落库）
                conversation_id,
                loop,
                None,  # cancel_token
            ),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        return False, "超时（>180s）", ""

    content = (result or {}).get("content") or ""
    traces = (result or {}).get("tool_traces") or []
    tool_names = [t.get("name") for t in traces]

    problems: list[str] = []

    # ---- 断言 1：是否调用了期望的工具 ----
    want_tools = expect.get("tools_any")
    if want_tools and not any(t in tool_names for t in want_tools):
        problems.append(f"未调用期望工具 {want_tools}，实际调用：{tool_names or '无'}")

    # ---- 断言 2：正文包含期望内容 ----
    want_any = expect.get("content_contains_any")
    if want_any and not any(w in content for w in want_any):
        problems.append(f"正文未包含任一期望内容 {want_any}")

    # ---- 断言 3：正文长度 ----
    min_len = expect.get("min_length")
    if min_len and len(content.strip()) < min_len:
        problems.append(f"正文过短（{len(content.strip())} < {min_len}）")

    # ---- 断言 4：幻觉守卫（条件断言）----
    if expect.get("hallucination_guard"):
        outcome = grounding.classify(traces)
        if outcome.attempted_but_failed:
            # 检索没拿到东西 → 必须老实承认
            if not any(m in content for m in DEGRADE_MARKERS):
                problems.append(
                    f"检索{outcome.status}（{outcome.reason}），"
                    f"但回答里没有任何降级话术 —— 疑似编造"
                )
        elif outcome.has_evidence:
            # 检索成功 → 引用的 URL 必须能在工具原文里找到
            unknown = grounding.unknown_urls(content, traces)
            if unknown:
                problems.append(f"引用了工具原文中不存在的 URL：{unknown[:3]}")

    # ---- 组装结果 ----
    head = content.strip().replace("\n", " ")[:90]
    if problems:
        detail = "\n".join(
            [
                f"问题：{p}" for p in problems
            ]
            + [f"工具：{tool_names or '无'}", f"回答：{head}…"]
        )
        return False, "；".join(problems), detail

    outcome = grounding.classify(traces)
    return (
        True,
        f"工具={tool_names or '无'} 检索={outcome.status} 回答：{head}…",
        "",
    )
