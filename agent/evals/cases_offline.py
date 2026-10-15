"""离线快评用例（P3 · 不调用模型，秒级，CI 可跑）。

每个 `test_*` 函数是一条用例：不抛异常 = 通过，抛 AssertionError = 失败。
返回值（字符串）会作为「补充说明」打印在用例名下面。

覆盖的是 P0/P1/P2 的**确定性成果** —— 这些地方一旦被改坏，
线上表现就是「又开始编造地址」「又开始重复搜索」「附件传上去模型看不见」，
但没有任何测试会报警。这一层就是它们的报警器。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from app.core.config import settings
from app.service import grounding


# =====================================================================
# P0：搜索强制引用 + 禁止无结果编造
# =====================================================================


EXA_OUTPUT = """1. 广州天河希尔顿酒店 (2024-05-01)
   来源: https://www.hilton.com/gz/tianhe
   摘要: 位于天河区体育西路，评分 4.6/5。

2. 广州天河城 (2024-04-02)
   来源: https://www.teemall.com.cn/
   摘要: 大型购物中心，地铁 1 号线体育中心站。
"""

TAVILY_OUTPUT = """1. 广州天气
   https://www.weather.com.cn/guangzhou
   广州今天多云转晴，气温 22-29 度。
"""


def test_grounding_parse_exa():
    """Exa 格式能被解析成结构化来源（含 URL 尾部句读剥离）。"""
    src = grounding.parse_sources(EXA_OUTPUT)
    assert len(src) == 2, f"应解析出 2 条，实际 {len(src)}"
    assert src[0]["url"] == "https://www.hilton.com/gz/tianhe", src[0]
    assert "希尔顿" in src[0]["title"], src[0]
    assert "4.6" in src[0]["snippet"], src[0]
    return f"{len(src)} 条来源，URL 正确剥离尾部句读"


def test_grounding_parse_tavily():
    """Tavily 格式（裸 URL 行 + 正文行）也能解析。"""
    src = grounding.parse_sources(TAVILY_OUTPUT)
    assert len(src) == 1, src
    assert src[0]["url"] == "https://www.weather.com.cn/guangzhou", src[0]
    return "Tavily 裸 URL 行解析正常"


def test_grounding_classify_outcomes():
    """ok / empty / failed / none 四种判定不能串。"""
    ok = grounding.classify_single("web_search", EXA_OUTPUT)
    empty = grounding.classify_single("web_search", "未检索到相关网页结果")
    failed = grounding.classify_single("web_search", "联网搜索失败：timeout")
    none = grounding.classify_single("calculator", "4")
    assert ok == "ok", ok
    assert empty == "empty", empty
    assert failed == "failed", failed
    assert none == "none", none
    return "ok/empty/failed/none 判定正确"


def test_grounding_enforce_appends_unverified_note():
    """检索失败时必须给用户追「未核实」降级说明 —— 这是反编造的最后一道。"""
    traces = [{"name": "web_search", "result": "联网搜索失败：timeout"}]
    out = grounding.enforce("天河区最好的酒店是 X。", traces)
    # 注意：降级话术原文是「未经外部来源核实」，**不含**「未核实」这个连续子串。
    # 断言必须照实际文案写，否则用例会因为笔误永远红（第一版就踩了这个坑）。
    assert "未经外部来源核实" in out, out
    assert "⚠️" in out, out
    return "检索失败 → 已追加未核实说明"


def test_grounding_enforce_appends_sources_when_missing():
    """检索成功但正文一个来源都没引 → 自动补来源块。"""
    traces = [{"name": "web_search", "result": EXA_OUTPUT}]
    out = grounding.enforce("天河区有几家不错的酒店。", traces)
    assert "检索来源" in out, out
    assert "hilton.com" in out, out
    return "正文未引用 → 已补来源块"


def test_grounding_enforce_keeps_existing_citation():
    """正文已经引了来源 → 不再重复补一坨来源块。"""
    traces = [{"name": "web_search", "result": EXA_OUTPUT}]
    out = grounding.enforce("推荐 [希尔顿](https://www.hilton.com/gz/tianhe)。", traces)
    assert "检索来源" not in out, f"已引用仍补了来源块：{out}"
    return "已引用 → 不重复补"


def test_grounding_enforce_ignores_chitchat():
    """没调用过搜索的闲聊不能被打扰（不能给「你好」也贴未核实）。"""
    out = grounding.enforce("你好呀", [])
    assert out == "你好呀", f"闲聊被改动了：{out}"
    return "未检索 → 原样返回"


def test_grounding_unknown_urls_detected():
    """正文里出现工具原文没有的 URL = 可疑引用，必须能识别出来。"""
    traces = [{"name": "web_search", "result": EXA_OUTPUT}]
    unknown = grounding.unknown_urls(
        "详见 https://www.fake-hotel-booking.com/x", traces
    )
    assert unknown and "fake-hotel-booking.com" in unknown[0], unknown
    return f"可疑 URL 已识别：{unknown[0]}"


def test_grounding_reminder_only_when_no_evidence():
    """提醒只在检索无果时注入；有证据时不能打扰模型。"""
    assert grounding.reminder_for("web_search", "联网搜索失败") is not None
    assert grounding.reminder_for("web_search", EXA_OUTPUT) is None
    assert grounding.reminder_for("calculator", "4") is None
    return "提醒仅在 empty/failed 时注入"


# =====================================================================
# P1：相似 query 不二次搜索 + 单轮搜索预算
# =====================================================================


def test_search_cache_exact_and_similar():
    """改写的 query 必须命中相似缓存 —— 这是 P1 省掉重复搜索的关键。

    注意用 `get_with_meta`：`get()` 按设计**只返回精确命中**（保留旧行为），
    相似命中要看返回的 kind。线上 web_search 走的是 get_with_meta。
    """
    from app.tool import search_cache

    conv = "eval-conv-cache"
    search_cache.put(conv, "广州天河区酒店推荐", 5, "RESULT-A")

    exact = search_cache.get_with_meta(conv, "广州天河区酒店推荐", 5)
    assert exact is not None and exact[1] == "exact", f"精确命中失败：{exact}"

    # 换个近义词也要命中（这是 P1 的核心：模型最爱改写 query）
    similar = search_cache.get_with_meta(conv, "广州天河酒店", 5)
    assert similar is not None, "相似命中失败（模型改写 query 会重复搜索）"
    assert similar[1] == "similar", f"命中类型应为 similar，实际 {similar[1]}"
    return "exact + similar 均命中（改写 query 不再重复搜索）"


def test_search_cache_no_false_positive():
    from app.tool import search_cache

    conv = "eval-conv-nofp"
    search_cache.put(conv, "广州酒店", 5, "RESULT-A")
    assert search_cache.get_with_meta(conv, "北京烤鸭店", 5) is None, "不同主题被误判为相似"
    # 旧结果条数不够时不能拿来顶替
    assert search_cache.get_with_meta(conv, "广州酒店", 8) is None, "top_k 更大时不应复用旧结果"
    # 不能跨会话串味
    assert search_cache.get_with_meta("eval-other-conv", "广州酒店", 5) is None, "跨会话串味"
    return "不误命中 / 不跨会话 / top_k 不足不复用"


def test_search_budget_caps_web_search_only():
    """单轮搜索预算只掐 web_search，不能顺手把计算器也掐了。"""
    from app.agent.agent import _SearchBudget

    budget = _SearchBudget(2)
    assert budget.take("web_search") is None
    assert budget.take("web_search") is None
    assert budget.take("web_search") is not None, "第 3 次搜索应被拦下"
    assert budget.take("calculator") is None, "非搜索工具不该被预算影响"
    return "第 3 次搜索被拦，calculator 不受限"


# =====================================================================
# P2：真任务 worker
# =====================================================================


def test_task_worker_query_parsing_robust():
    """LLM 输出 JSON 从不老实，四种脏输入都得能抽出查询词。"""
    from app.service.task_worker import _parse_queries

    assert _parse_queries('["广州天河酒店", "广州天河区住宿"]', "fb", 3) == [
        "广州天河酒店",
        "广州天河区住宿",
    ]
    # 套了 ```json 代码块
    assert _parse_queries('```json\n["a","b"]\n```', "fb", 3) == ["a", "b"]
    # 直接给编号列表
    assert _parse_queries("1. 酒店推荐\n2. 交通枢纽", "fb", 3) == [
        "酒店推荐",
        "交通枢纽",
    ]
    # 完全乱来 → 退化成 goal
    assert _parse_queries("", "兜底目标", 3) == ["兜底目标"]
    # 去重 + 截断
    assert _parse_queries('["a","a","b","c","d"]', "f", 3) == ["a", "b", "c"]
    return "4 种脏输入 + 去重截断均正确"


def test_task_cancel_token_interrupts_sleep():
    """协作式取消必须能打断 sleep —— 否则用户点了取消任务还在跑。"""
    from app.service.task_worker import TaskCancelToken, TaskCancelled

    async def scenario():
        token = TaskCancelToken()

        async def canceller():
            await asyncio.sleep(0.05)
            token.cancel()

        task = asyncio.create_task(canceller())
        try:
            await token.sleep(10)
        except TaskCancelled:
            return True
        finally:
            await task
        return False

    assert asyncio.run(scenario()), "取消没有打断 sleep"
    return "sleep 被取消即时打断"


# =====================================================================
# P2：附件 / 多模态
# =====================================================================


def test_attachment_safe_name_blocks_traversal():
    from app.service.attachment_service import AttachmentService

    svc = AttachmentService()
    assert svc._safe_name("../../etc/passwd") == "passwd", "路径穿越未拦住"
    assert svc._safe_name("报告 2026:版本?.txt") == "报告 2026_版本_.txt"
    assert svc._safe_name("") == "unnamed"
    return "路径穿越与非法字符已过滤"


def test_attachment_text_encoding_fallback():
    """GBK 的 txt 不能因为解码失败就整份丢掉。"""
    from app.service.attachment_service import AttachmentService

    svc = AttachmentService()
    assert svc._extract_text("中文内容".encode("utf-8"), "text/plain", ".txt") == "中文内容"
    assert svc._extract_text("中文内容".encode("gbk"), "text/plain", ".txt") == "中文内容"
    # 非文本且非 PDF → 抽不出就返回 None（不能塞空串骗模型）
    assert svc._extract_text(b"\x00\x01\x02", "application/zip", ".zip") is None
    return "utf-8 / gbk 均可抽，非文本返回 None"


def test_attachment_build_context_image_switch():
    """图片多模态开关：开 → content part；关 → 退化成占位文本（不能 400）。"""
    from app.service.attachment_service import AttachmentService

    svc = AttachmentService()
    rows = [
        {"name": "a.txt", "mime": "text/plain", "size_bytes": 10,
         "extracted_text": "文件内容ABC", "storage_path": None, "attachment_id": "1"},
        {"name": "x.png", "mime": "image/png", "size_bytes": 2048,
         "extracted_text": None, "storage_path": None, "attachment_id": "2"},
    ]

    orig = settings.ATTACHMENT_IMAGE_ENABLED
    try:
        settings.ATTACHMENT_IMAGE_ENABLED = True
        text, parts = svc.build_context(rows)
        assert "文件内容ABC" in text
        # storage_path 为空 → 读不到图，不应生成 part（也不能崩）
        assert isinstance(parts, list)

        settings.ATTACHMENT_IMAGE_ENABLED = False
        text2, parts2 = svc.build_context(rows)
        assert parts2 == [], "关闭后不应产出 image part"
        assert "未开启图片理解" in text2, text2
    finally:
        settings.ATTACHMENT_IMAGE_ENABLED = orig
    return "图片开关两种行为均正确"


def test_attachment_write_file_uses_attachment_id():
    """落盘文件名必须是 attachment_id，不能用用户文件名（防覆盖/穿越）。"""
    from app.service.attachment_service import AttachmentService

    svc = AttachmentService()
    tmp = tempfile.mkdtemp()
    orig = settings.ATTACHMENT_DIR
    try:
        settings.ATTACHMENT_DIR = tmp
        rel = svc._write_file(7, "att-abc-123", ".txt", b"hello")
        assert rel == "7/att-abc-123.txt", rel
        assert os.path.exists(os.path.join(tmp, rel))
    finally:
        settings.ATTACHMENT_DIR = orig
    return f"落盘路径 {rel}（用 attachment_id 命名）"


# =====================================================================
# P2：多模态注入到 messages
# =====================================================================


def test_agent_build_messages_multimodal_shape():
    """有图片时 user content 必须是 content part 数组，字符串无效。"""
    from app.agent.agent import Agent

    agent = Agent()
    image_parts = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    ]
    msgs = agent._build_messages(
        message="看看这张图", attachment_text="【附件】x.png", image_parts=image_parts
    )
    user_msg = msgs[-1]
    assert user_msg["role"] == "user"
    assert isinstance(user_msg["content"], list), "图片场景下 content 必须是数组"
    assert user_msg["content"][0]["type"] == "text"
    assert "看看这张图" in user_msg["content"][0]["text"]
    assert "【附件】x.png" in user_msg["content"][0]["text"]
    assert user_msg["content"][1]["type"] == "image_url"

    # 无图片时退回纯字符串（否则非 vision 模型会 400）
    plain = agent._build_messages(message="你好")
    assert isinstance(plain[-1]["content"], str), "无附件时 content 应保持字符串"
    return "有图→数组 / 无图→字符串"
