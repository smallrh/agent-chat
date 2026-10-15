"""流式生成取消机制：一次流式对话 = 一个 run（用 run_id 标识）。

- 前端点击「停止生成」时，调用 POST /chat/cancel {run_id} 通知服务端。
- 服务端在流式生成过程中（每读一段 / 每个工具执行前）检查对应 token 是否已被取消，
  被取消则抛 AgentCancelled，提前结束生成并走正常尾部事件 + 落库。

边界说明：
- 当前用进程内字典实现，对单 worker / 开发热重载足够。
- 若部署为多 worker（uvicorn --workers N），需改为 Redis 共享（否则 cancel 可能落在
  不同进程而失效）。后续多实例部署时再替换 _REGISTRY 后端即可。
"""

import asyncio
import uuid


class AgentCancelled(Exception):
    """流式生成被用户取消。"""


class RunToken:
    __slots__ = ("run_id", "user_id", "_event")

    def __init__(self, run_id: str, user_id: int):
        self.run_id = run_id
        self.user_id = user_id
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


# run_id -> RunToken（进程内，单 worker 足够）
_REGISTRY: dict[str, RunToken] = {}


def create_token(user_id: int) -> RunToken:
    token = RunToken(str(uuid.uuid4()), user_id)
    _REGISTRY[token.run_id] = token
    return token


def get_token(run_id: str) -> RunToken | None:
    return _REGISTRY.get(run_id)


def remove_token(run_id: str) -> None:
    _REGISTRY.pop(run_id, None)
