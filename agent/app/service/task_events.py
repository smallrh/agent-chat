import asyncio
from typing import Any

"""
进程内任务事件总线（pub/sub）。

P2 需求：后台任务在另一段 asyncio 协程里跑，进度要实时推给前端。

设计：
  - 单个 FastAPI 进程 = 单事件循环，后台任务（asyncio.create_task）和
    SSE 订阅者（/tasks/stream）都在同一进程内，因此用「内存 queue」做 pub/sub
    足够，不需要 Redis/Kafka 中转。
  - 以 conversation_id 为频道：前端对某个会话开一条 SSE，订阅该会话下所有
    任务的 progress / completed / failed / cancelled 事件。
  - 事件结构统一为 dict：{"type": "task.progress", "data": {...}}，由 SSE
    端点用 sse() 编码后发出。

可扩展性：将来若多副本部署，把 publish 改成「写 outbox → Kafka → 各副本
消费 → 本地 bus」即可，订阅侧代码不变。
"""


class TaskEventBus:
    def __init__(self):
        # conversation_id -> set(asyncio.Queue)
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, conversation_id: str) -> asyncio.Queue:
        """订阅某会话的任务事件，返回一个 asyncio.Queue。"""
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(conversation_id, set()).add(q)
        return q

    def unsubscribe(self, conversation_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(conversation_id)
        if subs and q in subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(conversation_id, None)

    def publish(self, conversation_id: str, event_type: str, data: dict[str, Any]) -> None:
        """
        发布一条任务事件给该 conversation_id 的所有订阅者。

        data 里会自动补上 conversation_id，方便前端路由。
        """
        subs = self._subs.get(conversation_id)
        if not subs:
            return
        payload = {"conversation_id": conversation_id, **data}
        for q in list(subs):
            try:
                q.put_nowait({"type": event_type, "data": payload})
            except Exception:
                # 某个队列满了/坏了不影响其它订阅者
                subs.discard(q)


# 模块级单例
task_event_bus = TaskEventBus()
