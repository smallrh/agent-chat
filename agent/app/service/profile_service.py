import logging

from app.repo.memory_repo import MemoryRepository
from app.repo.redis_repo import RedisRepository

logger = logging.getLogger(__name__)


class ProfileService:
    """
    用户画像维护。

    Profile 是一个扁平 dict（放 Redis），记录位置、偏好、常用工具等结构化信息。
    MemoryWorker 每次跑完 memory 抽取后，把新抽到的 memory 合并进 profile。

    【键的设计：必须按 slot，不能按 memory_type】
    memory_type 是粗粒度类别（location/preference/...），同一类别下可以有多个
    **语义不同**的属性：
        "用户目前在广州上班"  → memory_type=location, slot=residence_city
        "我的籍贯是四川"      → memory_type=location, slot=hometown
    若按 memory_type 建键，二者都落到 location，后者会把前者覆盖掉，
    导致「问现居地答成籍贯」这类错误。按 slot 建键则可并存：
        residence_city=广州, hometown=四川
    同理 preference 下可并存 favorite_food / favorite_sport 等多个槽位。

    Redis 只作缓存：profile 本质上是 PG `memories` 的结构化投影，
    因此 Redis 为空（重启/过期/新用户/schema 升级）时会从 PG 重建并回填。
    """

    def __init__(
        self,
        redis_repo: RedisRepository,
        memory_repo: "MemoryRepository | None" = None,
    ):
        self.redis_repo = redis_repo
        # 延迟启动：只有在 Redis 未命中、需要回退重建时才会起 PG 连接池，
        # 避免给热路径（每次对话）平白增加一个连接池。
        self.memory_repo = memory_repo or MemoryRepository()
        self._pg_started = False

    # ----------------------------------------------------------
    # schema 版本：profile 键的语义变了就要 bump，触发按新规则重建
    #   1 = 按 memory_type 扁平键（旧，会互相覆盖）
    #   2 = 按 slot 细粒度键（当前）
    # ----------------------------------------------------------
    PROFILE_SCHEMA_VERSION = 2
    VERSION_KEY = "_schema_version"

    # 仅用于「该条 memory 没有 slot」时的兜底：把 memory_type 映射成可读键。
    # 注意：这只是兜底，**有 slot 时一律优先用 slot**。
    _MEMORY_TYPE_FALLBACK_KEY: dict[str, str] = {
        "location": "location",
        "preference": "preferences",
        "name": "name",
        "birthday": "birthday",
        "contact": "contact",
        "job": "job",
        "school": "school",
        "hobby": "hobbies",
    }

    # ----------------------------------------------------------
    # memory → profile 片段映射（抽取写入与回退重建共用）
    # ----------------------------------------------------------

    @staticmethod
    def _profile_key(memory: dict) -> str:
        """取 profile 键：优先 slot（细粒度），无 slot 才退回 memory_type。"""
        slot = (memory.get("slot") or "").strip()
        if slot:
            return slot
        memory_type = (memory.get("memory_type") or "").strip()
        if not memory_type:
            return ""
        return ProfileService._MEMORY_TYPE_FALLBACK_KEY.get(
            memory_type, memory_type
        )

    @staticmethod
    def _time_key(memory: dict):
        """排序键：按更新时间升序，保证后面（较新）的覆盖前面的。"""
        t = memory.get("updated_at") or memory.get("created_at")
        # None 排最前；datetime/str 统一转 str 比较（ISO 串字典序即时间序）
        return (0, "") if t is None else (1, str(t))

    @classmethod
    def build_profile_from_memories(cls, memories: list[dict]) -> dict:
        """
        把一批 memory 映射成 profile 片段。

        - 键 = slot（无 slot 时才用 memory_type 兜底）
        - **先按时间升序排序，因此同一键由最新的一条胜出**（而不是遍历顺序碰运气）
        - 空内容直接跳过，不写入空值
        """
        result: dict = {}
        ordered = sorted(memories or [], key=cls._time_key)
        for m in ordered:
            key = cls._profile_key(m)
            if not key:
                continue
            content = (m.get("content") or "").strip()
            if content:
                result[key] = content
        return result

    # ----------------------------------------------------------
    # PG 回退：Redis 为空 / schema 升级时从 memories 重建
    # ----------------------------------------------------------

    async def _ensure_pg(self) -> bool:
        """懒启动 PG 连接池；失败返回 False（调用方据此降级）。"""
        if self._pg_started:
            return True
        try:
            await self.memory_repo.start()
            self._pg_started = True
            return True
        except Exception:
            logger.exception("ProfileService: failed to start PG pool")
            return False

    async def rebuild_from_memories(
        self,
        user_id: int,
        limit: int = 200,
    ) -> dict:
        """
        从 PG memories 重建 profile 并回填 Redis。

        memories 表是权威存储且按 user_id 全量可查（不受 conversation 限制），
        所以这里天然拿到的是"跨所有会话"的用户事实。
        """
        try:
            if not await self._ensure_pg():
                return {}
            rows = await self.memory_repo.list_memories(
                user_id, active_only=True, limit=limit
            )
        except Exception:
            logger.exception(
                "ProfileService.rebuild_from_memories FAILED: user_id=%s", user_id
            )
            return {}

        profile = self.build_profile_from_memories(rows)
        # 打上 schema 版本，供下次读取时判断是否需要重建
        profile[self.VERSION_KEY] = self.PROFILE_SCHEMA_VERSION

        try:
            await self.redis_repo.set_profile(user_id, profile)
        except Exception:
            logger.exception(
                "ProfileService: failed to backfill profile to Redis: user_id=%s",
                user_id,
            )
        return profile

    # ----------------------------------------------------------
    # 读取（给注入端用）
    # ----------------------------------------------------------

    async def get_profile(self, user_id: int) -> dict:
        """
        读用户画像：优先 Redis 缓存；缺失或 schema 版本过时则从 PG 重建。

        任何异常都降级为 {} —— profile 只是"增强"，绝不能阻断对话。
        """
        try:
            profile = await self.redis_repo.get_profile(user_id)
        except Exception:
            logger.exception(
                "ProfileService.get_profile Redis read FAILED: user_id=%s", user_id
            )
            profile = None

        if profile:
            if profile.get(self.VERSION_KEY) == self.PROFILE_SCHEMA_VERSION:
                return profile
            logger.info(
                "Profile schema upgrade (%s -> %s), rebuilding: user_id=%s",
                profile.get(self.VERSION_KEY), self.PROFILE_SCHEMA_VERSION, user_id,
            )

        # 没有 / 版本过旧 → 从 PG 按新规则重建
        return await self.rebuild_from_memories(user_id)

    # ----------------------------------------------------------
    # 写入（MemoryWorker 抽取后调用）
    # ----------------------------------------------------------

    async def update(
        self,
        user_id: int,
        memories: list[dict],
    ):
        if not memories:
            return

        # 用 get_profile 而不是直接读 Redis：
        # 这样旧 schema 的缓存会先被重建（按 slot 键），再合并新抽取结果。
        profile = await self.get_profile(user_id)
        profile.pop(self.VERSION_KEY, None)

        profile.update(self.build_profile_from_memories(memories))
        profile[self.VERSION_KEY] = self.PROFILE_SCHEMA_VERSION

        await self.redis_repo.set_profile(user_id, profile)

        logger.info(
            "Profile updated: user_id=%s, keys=%s",
            user_id,
            [k for k in profile if k != self.VERSION_KEY],
        )
