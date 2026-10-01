import logging

from app.repo.redis_repo import RedisRepository

logger = logging.getLogger(__name__)


class ProfileService:
    """
    用户画像维护。

    Profile 是一个扁平 dict（放 Redis），记录位置、偏好、常用工具等结构化信息。
    MemoryWorker 每次跑完 memory 抽取后，把新抽到的 memory 合并进 profile。
    """

    def __init__(self, redis_repo: RedisRepository):
        self.redis_repo = redis_repo

    # 每个 memory_type 对应的 profile key
    _MEMORY_TYPE_TO_PROFILE_KEY: dict[str, str] = {
        "location": "location",
        "preference": "preferences",
        "name": "name",
        "birthday": "birthday",
        "contact": "contact",
        "job": "job",
        "school": "school",
        "hobby": "hobbies",
    }

    async def update(
        self,
        user_id: int,
        memories: list[dict],
    ):
        if not memories:
            return

        profile = await self.redis_repo.get_profile(user_id) or {}

        for m in memories:
            memory_type = m.get("memory_type", "")
            profile_key = self._MEMORY_TYPE_TO_PROFILE_KEY.get(
                memory_type, memory_type
            )
            profile[profile_key] = m.get("content", "")

        await self.redis_repo.set_profile(user_id, profile)

        logger.info(
            "Profile updated: user_id=%s, keys=%s",
            user_id,
            list(profile.keys()),
        )
