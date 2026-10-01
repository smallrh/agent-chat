import json
import logging
from typing import Any

from openai import AsyncOpenAI

from app.core.config import settings
from app.kafka.memory_trigger import MemoryTrigger
from app.repo.memory_repo import MemoryRepository
from app.repo.qdrant_repo import get_qdrant_repo
from app.repo.redis_repo import RedisRepository
from app.service.profile_service import ProfileService
from app.service.summary_service import SummaryService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM 抽取 prompt
# ---------------------------------------------------------------------------
_EXTRACTION_SYSTEM_PROMPT = """你是一个记忆抽取器。从给定的对话消息中，把关于**用户本人**的稳定信息抽取出来，
严格返回一个 JSON 对象：{"memories": [...]}。只抽对长期记忆有价值的内容，不要抽临时上下文、AI 回复、一次性问题。

每个 memory 对象必须包含以下字段：
  - memory_type: 字符串，必须是 {location, preference, name, birthday, contact, job, school, hobby, personality, goal} 之一
  - content: 字符串，具体的记忆内容（例如 "用户是 Java 开发工程师"）
  - importance: 0~1 的浮点数，1.0 = 极度重要，0.3 = 不太重要但值得记
  - slot: 字符串，这条记忆描述的"用户属性槽位"的英文 snake_case key，例如
          primary_language / residence_city / os / job_title / name / favorite_food / employer。
          同一个属性在不同时间的取值**必须用同一个 slot**，例如：
            "用户主要使用 Java 开发" 和 "用户目前主要使用 Go 开发" → 都用 primary_language
            "用户住在北京" 和 "用户目前住在深圳"         → 都用 residence_city
            "用户使用 Windows" 和 "用户已经切换到 macOS"  → 都用 os
          这样系统才能只保留该属性的最新值，避免新旧矛盾并存注入。
          如果实在无法归类，用 memory_type 的英文作为 slot（如 preference / location）。

抽取规则：
  1. 只抽**用户说的**内容，不抽 AI 助理的回复
  2. 必须是**稳定事实**，不要抽一次性问题（例如 "今天帮我写个脚本"）
  3. 不要重复，如果同一信息已经能明确推断，就只抽一次
  4. 同一 slot 的新旧取值，都各自抽成一条（不要自行合并），系统会按时间保留最新
  5. 如果没有任何值得抽的内容，返回 {"memories": []}
  6. **只返回 JSON 对象，不要加 markdown 代码块或解释文字**"""

_EXTRACTION_USER_TEMPLATE = """以下是一组对话消息：

{messages_text}

请从中抽取关于用户本人的稳定记忆，返回 {{"memories": [...]}} 格式的 JSON。"""

# ---------------------------------------------------------------------------
# Prompt 版本
# ---------------------------------------------------------------------------
# 每次修改 _EXTRACTION_SYSTEM_PROMPT / _EXTRACTION_USER_TEMPLATE 手动 bump。
# 用于在 PG 里区分"哪版 prompt 生成的记忆"，后续查错 / 对比 / 重跑都靠它。
# ---------------------------------------------------------------------------
_EXTRACTION_PROMPT_VERSION = "v1"


def _format_messages(messages: list[dict]) -> str:
    """把 message list 格式化成 LLM 可读的文本。"""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        # 截断过长内容，避免 token 爆
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


def _parse_memory_json(text: str) -> list[dict]:
    """
    从 LLM 返回里解析 memory JSON。

    期望格式：{"memories": [...]}（配合 response_format=json_object）
    也兼容直接返回数组 [...] 或包在 ```json ... ``` 里的情况。
    """
    text = text.strip()

    # 去掉可能的 markdown 代码块
    if text.startswith("```"):
        # 找第一个 { 或 [
        obj_start = text.find("{")
        arr_start = text.find("[")
        if obj_start >= 0 and (arr_start < 0 or obj_start < arr_start):
            text = text[obj_start:text.rfind("}") + 1]
        elif arr_start >= 0:
            text = text[arr_start:text.rfind("]") + 1]
        else:
            # 退一步：去掉 ``` 前后缀
            text = text.strip("`")
            text = text.strip("jsonJSON \n")

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 兜底：提取第一个 {...} 或 [...]
        obj_start = text.find("{")
        arr_start = text.find("[")
        if obj_start >= 0 and (arr_start < 0 or obj_start < arr_start):
            obj_end = text.rfind("}") + 1
            if obj_end > obj_start:
                try:
                    data = json.loads(text[obj_start:obj_end])
                except json.JSONDecodeError:
                    logger.warning("extract LLM response not JSON: %s", text[:200])
                    return []
        elif arr_start >= 0:
            arr_end = text.rfind("]") + 1
            if arr_end > arr_start:
                try:
                    data = json.loads(text[arr_start:arr_end])
                except json.JSONDecodeError:
                    logger.warning("extract LLM response not JSON: %s", text[:200])
                    return []
        else:
            logger.warning("extract LLM response not JSON: %s", text[:200])
            return []

    if data is None:
        return []

    # {"memories": [...]} → 取出数组
    if isinstance(data, dict):
        raw_list = data.get("memories") or data.get("items") or []
    elif isinstance(data, list):
        raw_list = data
    else:
        logger.warning("extract LLM response unexpected type: %s", type(data))
        return []

    if not isinstance(raw_list, list):
        return []

    # 校验字段 & 补默认值
    valid_types = {
        "location", "preference", "name", "birthday", "contact",
        "job", "school", "hobby", "personality", "goal",
    }
    cleaned: list[dict] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        mtype = item.get("memory_type", "")
        content = item.get("content", "")
        if not content:
            continue
        if mtype not in valid_types:
            mtype = "preference"  # 兜底归类
        try:
            importance = float(item.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.1, min(1.0, importance))
        # 槽位：同属性不同取值共用的 key；缺失时回退到 memory_type
        slot = item.get("slot")
        if not isinstance(slot, str) or not slot.strip():
            slot = mtype
        cleaned.append({
            "memory_type": mtype,
            "content": content,
            "importance": importance,
            "slot": slot.strip(),
        })

    return cleaned


# ---------------------------------------------------------------------------
# LLM 精判：两条 memory 是否"同一事实"（向量粗筛后，解决 embed 区分度差的问题）
# ---------------------------------------------------------------------------
_LLM_SAME_FACT_PROMPT = (
    "你是一个记忆去重判断器。给定两条关于用户的记忆，判断它们是否在表达**同一个事实**。\n"
    "规则：\n"
    "  - 同一事实（YES）：措辞不同但核心语义完全相同，如'用户喜欢吃辣'≈'用户爱吃辣椒'\n"
    "  - 不同事实（NO）：即使主题相近但对象/方向/否定词不同，如\n"
    "    '用户喜欢川菜' vs '用户喜欢湘菜'（不同菜系）\n"
    "    '用户讨厌吃辣' vs '用户喜欢吃辣'（方向相反）\n"
    "    '用户喜欢吃辣' vs '用户来自四川'（不同维度）\n"
    "只输出 YES 或 NO，不要解释。\n\n"
    "记忆类型：{mtype}\n"
    "记忆 A：{a}\n"
    "记忆 B：{b}\n"
    "答案："
)


async def _llm_same_fact(
    llm,
    content_a: str,
    content_b: str,
    mtype: str,
) -> bool:
    """
    用 LLM 判断两条 memory content 是否"同一事实"。
    用于解决 nomic-embed-text 把否定词/不同维度也拉成近向量的问题。

    Returns:
        True  → 同一事实 → 应该 merge
        False → 不同事实 → 各自保留
        任何异常 → False（保守，宁可不 merge）
    """
    try:
        resp = await llm.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "你是记忆去重判断器，只输出 YES 或 NO。"},
                {"role": "user", "content": _LLM_SAME_FACT_PROMPT.format(
                    mtype=mtype, a=content_a, b=content_b,
                )},
            ],
            max_tokens=4,   # YES/NO 就够
            temperature=0.0,
        )
        answer = (resp.choices[0].message.content or "").strip().upper()
        result = "YES" in answer
        logger.info(
            "_llm_same_fact: A=%.30s... B=%.30s... → %s (raw=%r)",
            content_a, content_b, "MERGE" if result else "KEEP", answer,
        )
        return result
    except Exception as e:
        logger.warning("_llm_same_fact failed → default NO: %s", e)
        return False  # 保守：LLM 异常时不 merge


class MemoryService:
    """
    Memory 提取 + 持久化。

    extract()      → DeepSeek deepseek-chat 从 messages 抽结构化 Memory
    embed()        → LM Studio 本地 nomic-embed-text 拿向量
    save()         → 串联：Redis 去重 → PG 写 memory_id → Embedding → Qdrant upsert

    两个 AsyncOpenAI client：
      - self.llm      → DeepSeek（chat/completions）
      - self.embedder → LM Studio（embeddings）
    """

    def __init__(
        self,
        redis_repo: RedisRepository,
        memory_repo: MemoryRepository,
        qdrant_repo: QdrantRepository,
    ):
        self.redis_repo = redis_repo
        self.memory_repo = memory_repo
        self.qdrant_repo = qdrant_repo

        # Chat（DeepSeek）
        self.llm = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        )

        # Embedding（LM Studio 本地 nomic-embed-text，OpenAI 兼容）
        self.embedder = AsyncOpenAI(
            api_key=settings.EMBEDDING_API_KEY,
            base_url=settings.EMBEDDING_BASE_URL,
        )

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> list[float] | None:
        """
        调 LM Studio 本地 nomic-embed-text 拿向量。
        失败返回 None（上层 graceful skip Qdrant）。
        """
        try:
            r = await self.embedder.embeddings.create(
                model=settings.EMBEDDING_MODEL,
                input=text[:2000],  # 安全截断
            )
            vec = r.data[0].embedding
            logger.info(
                "embed: dim=%d model=%s text=%.40s",
                len(vec), settings.EMBEDDING_MODEL, text,
            )
            return vec
        except Exception as e:
            logger.exception(
                "embed FAILED (model=%s): %s", settings.EMBEDDING_MODEL, e,
            )
            return None

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------

    async def extract(
        self,
        user_id: int,
        conversation_id: str,
        messages: list[dict],
        source_message_ids: list[str] | None = None,
    ) -> list[dict]:
        """
        从最近消息里抽取值得长期记忆的结构化条目。

        流程：构造 prompt → LLM 返回 JSON 数组 → 校验 → 补溯源字段。

        Args:
            source_message_ids: 这一批消息对应的 message_id 数组（evidence），
                                默认 None 时从 messages 里自动收集 role=user 的。
        """
        if not messages:
            return []

        # 自动收集证据消息 ID（只取 user 说的话）
        if source_message_ids is None:
            source_message_ids = [
                m["message_id"]
                for m in messages
                if m.get("role") == "user" and m.get("message_id")
            ]

        formatted = _format_messages(messages)

        call_messages = [
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": _EXTRACTION_USER_TEMPLATE.format(
                messages_text=formatted
            )},
        ]

        logger.debug(
            "========== MemoryService.extract() LLM CALL ==========\n"
            "model=%s prompt_version=%s max_tokens=1024 temperature=0.1\n"
            "user_id=%s conversation_id=%s input_messages=%d source_message_ids=%s\n"
            "=====================================================",
            settings.OPENAI_MODEL, _EXTRACTION_PROMPT_VERSION,
            user_id, conversation_id, len(messages), source_message_ids,
        )

        response = await self.llm.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=call_messages,
            max_tokens=1024,
            temperature=0.1,          # 抽取任务要稳定，低温度
            response_format={"type": "json_object"},
        )

        raw_text = response.choices[0].message.content or ""
        extracted = _parse_memory_json(raw_text)

        # 补上溯源字段（让每条 memory 可追溯"哪条消息 / 哪个模型 / 哪个 prompt 版本"）
        for m in extracted:
            m["user_id"] = user_id
            m["source_conversation_id"] = conversation_id
            m["source_message_ids"] = source_message_ids
            m["extract_model"] = settings.OPENAI_MODEL
            m["prompt_version"] = _EXTRACTION_PROMPT_VERSION

        logger.info(
            "MemoryService.extract: conversation_id=%s, extracted=%d items, "
            "model=%s prompt_version=%s source_msg_ids=%d",
            conversation_id, len(extracted),
            settings.OPENAI_MODEL, _EXTRACTION_PROMPT_VERSION,
            len(source_message_ids),
        )
        logger.debug("Extracted memories: %s", extracted)

        return extracted

    async def save(self, user_id: int, memory: dict):
        """
        保存单条 memory。完整链路：

          Embedding → 近似合并（同类型 cosine ≥ 0.85 跳过）
                     → PG INSERT ON CONFLICT DO NOTHING RETURNING memory_id
                     → Qdrant upsert
                     → mark_synced / mark_pending_failed
                     → Redis List 缓存

        近似合并: 在同 user + 同 memory_type 范围内，新记忆向量和已落库记忆
        cosine 相似度 ≥ MEMORY_MERGE_SIMILARITY_THRESHOLD (0.85) 时，
        判定为语义重复（"喜欢辣" ≈ "爱吃辣椒"）→ 新的被跳过，保留已落库那条。
        已落库那条的 PG 行 is_active=true，Qdrant is_active=true，
        语义检索天然只召回保留的那条。
        """
        # 0) 注入 embedding 模型名（溯源字段之一，embed 之前就知道）
        memory["embedding_model"] = settings.EMBEDDING_MODEL
        mtype = memory["memory_type"]
        content = memory["content"]

        # 1) Embedding（近似合并 + Qdrant 都要，先算）
        vec = await self.embed(content)

        # 2) 近似合并：向量粗筛 + LLM 精判
        if vec is not None:
            try:
                similar = await self.qdrant_repo.find_similar_in_type(
                    user_id=user_id,
                    memory_type=mtype,
                    vector=vec,
                    threshold=settings.MEMORY_MERGE_VECTOR_THRESHOLD,
                )

                # LLM 精判（解决 nomic-embed-text 把"讨厌"≈"喜欢"的问题）
                merge_target = None
                if similar and settings.MEMORY_MERGE_LLM_JUDGE:
                    for cand in similar[:3]:  # 最多判 3 个候选，控 token
                        try:
                            cand_row = await self.memory_repo.get_by_id(cand["memory_id"])
                        except Exception:
                            cand_row = None
                        if not cand_row:
                            continue
                        is_same = await _llm_same_fact(
                            self.llm, content, cand_row["content"], mtype
                        )
                        if is_same:
                            merge_target = cand
                            break
                elif similar:
                    # 不开 LLM 精判 → 纯向量粗筛就 merge（有风险但省 token）
                    merge_target = similar[0]

                if merge_target:
                    logger.info(
                        "MemoryService.save: MERGE (vector_sim=%.4f ≥ %.2f) "
                        "SKIP new content=%.40s → keep memory_id=%s",
                        merge_target["score"],
                        settings.MEMORY_MERGE_VECTOR_THRESHOLD,
                        content,
                        merge_target["memory_id"],
                    )
                    # 近似合并命中 → 整条链路跳过
                    # （精确去重 + Qdrant + Redis 都不需要，保留的那条已经有）
                    return
            except Exception:
                # find_similar 失败不阻塞主链路，降级继续
                logger.exception(
                    "MemoryService.save: find_similar_in_type FAILED "
                    "(gracefully skip merge check) user_id=%s",
                    user_id,
                )

        # 3) PG 落库 → 拿到 memory_id（精确冲突时返回 None，安全短路）
        memory_id = None
        try:
            memory_id = await self.memory_repo.save(user_id, memory)
        except Exception as e:
            logger.exception("MemoryService.save: PG FAILED user_id=%s: %s", user_id, e)

        if not memory_id:
            logger.info(
                "MemoryService.save: SKIP (PG conflict / PG error) "
                "user_id=%s type=%s",
                user_id, mtype,
            )
            return

        # 4) Qdrant（近似合并已跳过，正常 upsert）
        embed_ok = vec is not None
        qdrant_ok = False
        qdrant_err: str | None = None

        if embed_ok:
            try:
                await self.qdrant_repo.upsert(
                    memory_id,
                    vec,
                    user_id=user_id,
                    memory_type=mtype,
                    importance=memory.get("importance", 0.5),
                    is_active=True,
                    slot=memory.get("slot"),
                )
                qdrant_ok = True
            except Exception as e:
                qdrant_err = f"Qdrant upsert failed: {type(e).__name__}: {e}"
                logger.exception("MemoryService.save: Qdrant FAILED memory_id=%s", memory_id)

        # 5) 根据结果更新 PG.sync_status
        if embed_ok and qdrant_ok:
            try:
                await self.memory_repo.mark_synced(memory_id)
            except Exception:
                logger.exception("MemoryService.save: mark_synced FAILED memory_id=%s", memory_id)
        else:
            reason = (
                "Embedding returned None (LM Studio unreachable?)"
                if not embed_ok
                else qdrant_err or "unknown"
            )
            try:
                await self.memory_repo.mark_pending_failed(memory_id, reason)
            except Exception:
                logger.exception("MemoryService.save: mark_pending_failed FAILED memory_id=%s", memory_id)

        # 4) Redis List 缓存（纯缓存，PG 已保证不重复）
        try:
            await self.redis_repo.push_memory(user_id, memory)
        except Exception as e:
            logger.warning("MemoryService.save: Redis cache push FAILED user_id=%s: %s", user_id, e)

        logger.info(
            "MemoryService.save: %s memory_id=%s user_id=%s type=%s content=%.50s",
            "INDEXED" if (embed_ok and qdrant_ok) else "PENDING",
            memory_id, user_id, memory["memory_type"], memory["content"],
        )


class MemoryWorker:
    """
    消费 memory.tasks topic，执行 Summary / Memory / Profile 三件套。

    执行流程：
        1. 从 Redis 读最近 20 条消息
        2. Summary → Redis
        3. Trigger 判断是否需要做 Memory 抽取
        4. Memory extract + save（PG + Qdrant + Redis）
        5. Profile 更新（Redis）
    """

    def __init__(self):
        self.redis_repo = RedisRepository()
        self.memory_repo = MemoryRepository()
        self.qdrant_repo = get_qdrant_repo()

        self.trigger = MemoryTrigger()
        self.summary_service = SummaryService(self.redis_repo)
        self.memory_service = MemoryService(
            self.redis_repo,
            self.memory_repo,
            self.qdrant_repo,
        )
        self.profile_service = ProfileService(self.redis_repo)

    # =========================
    # 生命周期
    # =========================

    async def start(self):
        await self.redis_repo.start()
        await self.memory_repo.start()
        await self.qdrant_repo.start()

        logger.info("MemoryWorker started")

    async def stop(self):
        await self.qdrant_repo.stop()
        await self.memory_repo.stop()
        await self.redis_repo.stop()

        logger.info("MemoryWorker stopped")

    # =========================
    # 入口
    # =========================

    async def handle_task(self, task: dict):
        task_type = task["task_type"]

        if task_type == "memory.process":
            await self.process_memory(task)
        else:
            logger.warning("Unknown memory task type: %s", task_type)

    async def process_memory(self, task: dict):
        user_id = task["user_id"]
        conversation_id = task["conversation_id"]

        logger.info(
            "[MemoryWorker] process_memory START: "
            "user_id=%s conversation_id=%s task=%s",
            user_id, conversation_id, task,
        )

        # 1. 拉最近消息
        # print(f"  [MW] ▶ BEFORE get_recent_messages conv={conversation_id}", flush=True)
        messages = await self.redis_repo.get_recent_messages(
            conversation_id,
            limit=20,
        )
        # print(f"  [MW] ▶ AFTER  get_recent_messages count={len(messages)}", flush=True)

        logger.info(
            "[MemoryWorker] Redis recent messages: count=%d, contents=%s",
            len(messages),
            [m.get("content", "")[:60] for m in messages],
        )

        # 2. Summary（每批都增量更新一次）
        # print(f"  [MW] ▶ BEFORE summary.generate", flush=True)
        existing = await self.redis_repo.get_summary(conversation_id)
        summary = await self.summary_service.generate(
            messages,
            existing_summary=existing,
        )
        # print(f"  [MW] ▶ AFTER  summary.generate summary={'YES' if summary else 'None'}", flush=True)

        if summary:
            await self.redis_repo.set_summary(conversation_id, summary)
            logger.info("Summary updated: conversation_id=%s", conversation_id)

        # 3. Trigger 判断是否做 Memory 抽取
        # print(f"  [MW] ▶ BEFORE trigger.should_extract", flush=True)
        should = await self.trigger.should_extract(messages)
        # print(f"  [MW] ▶ AFTER  trigger.should_extract -> {should}", flush=True)
        logger.info(
            "[MemoryWorker] should_extract -> %s (cycle_threshold=%d, count=%d)",
            should, self.trigger.cycle_threshold, len(messages),
        )
        if not should:
            logger.info(
                "[MemoryWorker] SKIP memory extraction: conversation_id=%s",
                conversation_id,
            )
            return

        # 4. Memory 抽取
        # print(f"  [MW] ▶ BEFORE memory_service.extract", flush=True)
        memories = await self.memory_service.extract(
            user_id=user_id,
            conversation_id=conversation_id,
            messages=messages,
        )
        # print(f"  [MW] ▶ AFTER  memory_service.extract count={len(memories)}", flush=True)

        for m in memories:
            await self.memory_service.save(user_id, m)

        # 5. Profile 更新
        # print(f"  [MW] ▶ BEFORE profile_service.update", flush=True)
        await self.profile_service.update(user_id, memories)
        # print(f"  [MW] ▶ AFTER  profile_service.update", flush=True)

        logger.info(
            "[MemoryWorker] DONE: conversation_id=%s, memories=%d",
            conversation_id, len(memories),
        )

memory_worker = MemoryWorker()
