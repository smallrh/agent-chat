from app.repo.conversation_repo import ConversationRepository
from app.repo.message_repo import MessageRepository


class ConversationService:
    def __init__(self):
        self._msg_repo = MessageRepository()
        self._conv_repo = ConversationRepository(self._msg_repo)
        self._started = False

    async def _ensure(self):
        if self._msg_repo is None:
            # aclose() 后置 None，这里兜底重建
            self._msg_repo = MessageRepository()
            self._conv_repo = ConversationRepository(self._msg_repo)
        if not self._started:
            await self._conv_repo.start()
            self._started = True

    async def aclose(self):
        """
        释放资源，由 lifespan shutdown 调用。

        PG 用的是共享池（app/repo/pg.py），由 lifespan 统一关闭，这里只解引用；
        原先这里什么都没关，热重载时 Pool 一路累积。
        """
        if self._conv_repo is not None:
            await self._conv_repo.stop()
        self._msg_repo = None
        self._conv_repo = None
        self._started = False

    async def list_conversations(self, user_id: int, limit: int = 50, offset: int = 0):
        await self._ensure()
        return await self._conv_repo.list_conversations(user_id, limit=limit, offset=offset)

    async def get_messages(self, conversation_id: str, user_id: int, limit: int = 200, offset: int = 0):
        await self._ensure()
        return await self._conv_repo.list_messages(
            conversation_id, user_id, limit=limit, offset=offset,
        )

    async def rename(self, conversation_id: str, user_id: int, title: str):
        await self._ensure()
        return await self._conv_repo.update_conversation_title(conversation_id, user_id, title)

    async def delete(self, conversation_id: str, user_id: int):
        await self._ensure()
        await self._conv_repo.soft_delete_conversation(conversation_id, user_id)

    async def set_title_if_empty(self, conversation_id: str, user_id: int, title: str):
        await self._ensure()
        await self._conv_repo.set_title_if_empty(conversation_id, user_id, title)


conversation_service = ConversationService()
