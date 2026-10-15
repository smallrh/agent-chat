import asyncio
import logging
import uuid

from app.core.config import settings
from app.repo.task_repo import (
    TaskRepository,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCEEDED,
)
from app.service.message_service import message_service
from app.service.task_events import task_event_bus
from app.service.usage_service import METRIC_TASK, usage_service
from app.service.task_worker import (
    TaskCancelToken,
    TaskCancelled,
    register_default_handlers,
)


logger = logging.getLogger(__name__)


class TaskLimitReached(Exception):
    """并发上限：该用户（或全局）在跑的任务太多了。

    API 层捕获后转 429，前端提示「已有太多任务在跑，请等一会」。
    """


class TaskService:
    """
    多任务核心服务。

    职责：
      1. spawn_task：并发校验 → 建 DB 行 → 发布 task.created → 后台跑 handler。
      2. _run：串起「置 running → 逐步 progress（入库 + 发事件）→
         成功 finish + 后台写回会话消息 / 失败 / 取消」。
      3. cancel：协作式取消（置 token）+ asyncio 取消（兜底）。
      4. retry：对终态任务（failed/cancelled/中断）重新起一轮。

    后台写回（P2 核心之一）：任务成功后的结果通过 message_service
    （outbox → Kafka → consumer → PG/Redis）写回原会话，作为一条
    assistant 消息落库 —— 即使客户端早已关掉 SSE，结果也不会丢。
    """

    def __init__(self):
        self._repo = TaskRepository()
        self._started = False
        # task_id -> asyncio.Task，用于 cancel
        self._running: dict[str, asyncio.Task] = {}
        # task_id -> TaskCancelToken，handler 内部的协作式取消检查点
        self._cancels: dict[str, TaskCancelToken] = {}
        # 注册的 handler：kind -> coroutine 函数
        self._handlers: dict[str, callable] = {}
        # handler 从 app/service/task_worker.py 注册进来（新增类型只改那一处）
        register_default_handlers(self)

    # ----------------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------------

    async def start(self):
        if not self._started:
            await self._repo.start()
            # 自愈：上次进程遗留的未终态任务（进程内协程已随进程消失），
            # 置为 failed 并补发事件，避免前端进度条永远转圈。
            await self._recover_interrupted()
            self._started = True

    async def _recover_interrupted(self) -> None:
        """
        进程启动时扫一次 status IN ('pending','running') 的旧行 → failed。

        背景与 outbox_relay.recover_stale_processing 完全同构：
        内存态任务不可跨进程存活，DB 里必须留下正确的终态。
        """
        try:
            rows = await self._repo.recover_interrupted()
        except Exception:
            logger.exception(
                "TaskService recover_interrupted FAILED (ignored, task "
                "self-healing is best-effort)",
            )
            return

        for row in rows:
            conversation_id = row.get("conversation_id")
            task_id = row.get("task_id")
            if not conversation_id or not task_id:
                continue
            error = "interrupted by restart"
            self._publish(conversation_id, "task.failed", {
                "task_id": task_id,
                "title": row.get("title"),
                "error": error,
                "recovered": True,
            })
            logger.warning(
                "TaskService recovered interrupted task: task_id=%s", task_id,
            )

    async def stop(self):
        # 先置协作式取消令牌，让 handler 有机会在检查点干净退出
        for tok in list(self._cancels.values()):
            tok.cancel()
        # 再取消所有在跑的 asyncio.Task
        for t in list(self._running.values()):
            t.cancel()
        self._running.clear()
        self._cancels.clear()
        if self._started:
            await self._repo.stop()
            self._started = False

    # ----------------------------------------------------------------
    # 对外暴露 repo（供 API 层复用，避免第二份连接池）
    # ----------------------------------------------------------------

    @property
    def repo(self) -> TaskRepository:
        """
        TaskService 持有的 TaskRepository 实例。

        API 层（app/api/tasks.py）统一通过本属性访问仓储，确保只存在
        一个连接池 —— 该池由 lifespan 中 task_service.start() 负责启动。
        切勿再从 app.repo.task_repo 另建模块级单例（那样不会有 start()，
        所有 /tasks 端点会直接 RuntimeError 500）。
        """
        return self._repo

    @property
    def is_ready(self) -> bool:
        """仓储连接池是否已就绪（lifespan 启动失败时用于降级提示）。"""
        return self._repo.pool is not None

    @property
    def kinds(self) -> list[str]:
        """已注册的任务类型（给前端渲染下拉/图标用）。"""
        return sorted(self._handlers.keys())

    # ----------------------------------------------------------------
    # handler 注册
    # ----------------------------------------------------------------

    def register_handler(self, kind: str, handler: callable) -> None:
        self._handlers[kind] = handler

    # ----------------------------------------------------------------
    # 对外：创建并后台启动任务
    # ----------------------------------------------------------------

    async def spawn_task(
        self,
        conversation_id: str,
        user_id: int,
        title: str,
        goal: str,
        kind: str = "demo",
    ) -> str:
        """
        创建任务并立即后台运行。

        返回 task_id（UUID 字符串）。本函数不阻塞等待任务完成，
        调用方（REST 接口 / Agent 工具）拿到 task_id 即可返回。

        Raises:
            ValueError: 未知 kind
            TaskLimitReached: 并发超限
        """
        if kind not in self._handlers:
            raise ValueError(f"unknown task kind: {kind}")

        await self._check_concurrency(user_id)

        task_id = str(uuid.uuid4())
        await self._repo.create_task(
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
            title=title,
            goal=goal,
            kind=kind,
        )
        token = TaskCancelToken()
        self._cancels[task_id] = token

        # P3 计费埋点：建任务即计一次（后台任务会真实消耗 LLM + 检索配额）
        await usage_service.incr(user_id, METRIC_TASK)

        self._publish(conversation_id, "task.created", {
            "task_id": task_id,
            "title": title,
            "goal": goal,
            "status": TASK_STATUS_PENDING,
            "kind": kind,
            "progress": 0,
            "step": "",
        })

        # 后台跑（必须在运行中的事件循环里调用 asyncio.create_task）
        t = asyncio.create_task(
            self._run(
                task_id=task_id,
                conversation_id=conversation_id,
                user_id=user_id,
                title=title,
                goal=goal,
                kind=kind,
                token=token,
            ),
            name=f"task-{task_id}",
        )
        self._running[task_id] = t
        t.add_done_callback(lambda _: self._on_done(task_id))
        return task_id

    def _on_done(self, task_id: str) -> None:
        self._running.pop(task_id, None)
        self._cancels.pop(task_id, None)

    async def _check_concurrency(self, user_id: int) -> None:
        """并发上限校验：per-user + global。任一项超限即拒绝新建。"""
        per_user = int(settings.TASK_MAX_ACTIVE_PER_USER or 0)
        if per_user > 0:
            n = await self._repo.count_active(user_id=user_id)
            if n >= per_user:
                raise TaskLimitReached(
                    f"too many active tasks for user (limit={per_user})"
                )

        global_limit = int(settings.TASK_MAX_ACTIVE_GLOBAL or 0)
        if global_limit > 0:
            n = await self._repo.count_active()
            if n >= global_limit:
                raise TaskLimitReached(
                    f"too many active tasks globally (limit={global_limit})"
                )

    # ----------------------------------------------------------------
    # 对外：取消任务
    # ----------------------------------------------------------------

    async def cancel_task(self, task_id: str, user_id: int) -> bool:
        """
        取消任务。返回 True=已取消；False=任务已终态或不存在。

        两条路径同时走，互补：
          1) 协作式：置 token → handler 在下一个检查点抛 TaskCancelled。
             （必需：web_search 是 to_thread 的同步调用，asyncio 取消打不断它）
          2) asyncio：Task.cancel() 兜底（handler 恰好卡在 await 点时立即生效）。
        """
        # 先置 token：handler 即便正在同步调用里，下一检查点也会退出
        token = self._cancels.get(task_id)
        if token is not None:
            token.cancel()

        ok = await self._repo.cancel(task_id, user_id)
        if ok:
            t = self._running.get(task_id)
            if t is not None and not t.done():
                t.cancel()
            # 从 DB 读 conversation_id 用于发布事件
            row = await self._repo.get_task(task_id)
            if row:
                self._publish(row["conversation_id"], "task.cancelled", {
                    "task_id": task_id,
                })
        return ok

    # ----------------------------------------------------------------
    # 对外：重跑一个终态任务
    # ----------------------------------------------------------------

    async def retry_task(self, task_id: str, user_id: int) -> bool:
        """
        重跑一个已终态（failed / cancelled）的任务。

        典型场景：进程重启被标成 "interrupted by restart"、或检索服务抖了一下
        导致整条任务失败。重跑复用原 title/goal/kind/conversation，
        进度与 result 清零，task_id 不变（前端卡片不用换 key）。

        返回 False = 任务不存在 / 不属于该用户 / 仍在进行中。
        """
        row = await self._repo.get_task(task_id)
        if row is None:
            return False
        if int(row["user_id"]) != user_id:
            return False
        if row.get("status") in (TASK_STATUS_PENDING, TASK_STATUS_RUNNING):
            return False

        kind = row.get("kind") or "demo"
        if kind not in self._handlers:
            return False

        await self._check_concurrency(user_id)

        conversation_id = row["conversation_id"]
        # 复位：状态回 pending、进度清零、清掉旧的 result/error
        await self._repo.finish_task(
            task_id,
            TASK_STATUS_PENDING,
            result=None,
            error=None,
            step="等待重跑",
        )
        await self._repo.update_progress(task_id, 0, step="等待重跑",
                                         steps_done=0, steps_total=0)

        token = TaskCancelToken()
        self._cancels[task_id] = token

        # 重跑同样消耗资源，计一次
        await usage_service.incr(int(row["user_id"]), METRIC_TASK)

        self._publish(conversation_id, "task.created", {
            "task_id": task_id,
            "title": row.get("title"),
            "goal": row.get("goal"),
            "status": TASK_STATUS_PENDING,
            "kind": kind,
            "progress": 0,
            "step": "等待重跑",
            "retried": True,
        })

        t = asyncio.create_task(
            self._run(
                task_id=task_id,
                conversation_id=conversation_id,
                user_id=int(row["user_id"]),
                title=row.get("title") or "",
                goal=row.get("goal") or "",
                kind=kind,
                token=token,
            ),
            name=f"task-{task_id}-retry",
        )
        self._running[task_id] = t
        t.add_done_callback(lambda _: self._on_done(task_id))
        return True

    # ----------------------------------------------------------------
    # 后台 runner
    # ----------------------------------------------------------------

    async def _run(
        self,
        task_id: str,
        conversation_id: str,
        user_id: int,
        title: str,
        goal: str,
        kind: str,
        token: TaskCancelToken,
    ) -> None:
        handler = self._handlers.get(kind, self._handlers["demo"])
        try:
            token.raise_if_cancelled()
            await self._repo.set_running(task_id)
            self._publish(conversation_id, "task.running", {
                "task_id": task_id,
                "title": title,
                "kind": kind,
            })

            async def progress(done: int, total: int, message: str = "") -> None:
                # 进度回调也是一个取消检查点：worker 忘了自己检查时，
                # 至少每上报一次进度就会被拦下来。
                token.raise_if_cancelled()
                pct = int(round(done / total * 100)) if total else 0
                await self._repo.update_progress(
                    task_id, pct,
                    step=message or None,
                    steps_done=done,
                    steps_total=total,
                )
                self._publish(conversation_id, "task.progress", {
                    "task_id": task_id,
                    "progress": pct,
                    "step": message or "",
                    "steps_done": done,
                    "steps_total": total,
                })

            result = await handler(
                task_id=task_id,
                progress=progress,
                cancel_token=token,
                conversation_id=conversation_id,
                user_id=user_id,
                goal=goal,
                title=title,
            )

            # 竞态保护：handler 慢跑期间用户可能点了取消（DB 已被置 cancelled）。
            # 这时不能把终态覆盖回 succeeded，否则前端会显示「已完成」
            # 而用户明明已取消 —— 以 DB 的 cancelled 为准。
            token.raise_if_cancelled()
            current = await self._repo.get_task(task_id)
            if current and current.get("status") == TASK_STATUS_CANCELLED:
                raise TaskCancelled("cancelled during execution")

            await self._finalize(
                task_id, TASK_STATUS_SUCCEEDED, result=result, step="已完成",
            )
            try:
                await message_service.create_assistant_message(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    content=self._format_result(title, goal, result),
                )
            except Exception:
                # 写回失败不能让任务本身变失败（结果已落 tasks 表）
                logger.exception(
                    "TaskService._run write-back message FAILED: task_id=%s",
                    task_id,
                )
            self._publish(conversation_id, "task.completed", {
                "task_id": task_id,
                "result": result,
                "step": "已完成",
            })
            logger.info("TaskService._run SUCCEEDED: task_id=%s kind=%s", task_id, kind)

        except (asyncio.CancelledError, TaskCancelled):
            await self._finalize(task_id, TASK_STATUS_CANCELLED, step="已取消")
            self._publish(conversation_id, "task.cancelled", {
                "task_id": task_id,
            })
            logger.info("TaskService._run CANCELLED: task_id=%s", task_id)

        except Exception as exc:
            await self._finalize(
                task_id, TASK_STATUS_FAILED,
                error=str(exc)[:500], step="执行失败",
            )
            self._publish(conversation_id, "task.failed", {
                "task_id": task_id,
                "error": str(exc)[:500],
            })
            logger.exception("TaskService._run FAILED: task_id=%s", task_id)

    # ----------------------------------------------------------------
    # 终态写入（尽量不被取消打断）
    # ----------------------------------------------------------------

    async def _finalize(
        self,
        task_id: str,
        task_status: str,
        result: dict | None = None,
        error: str | None = None,
        step: str | None = None,
    ) -> None:
        """
        写终态。用 shield 包一层，避免「取消刚打进来 → 终态没写进 DB」
        导致前端进度条永远转圈。

        shield 的语义：外层被取消时内层 Task 继续跑完；这里再补一次
        直接 await，把「写入完成」这件事尽量做实。
        """
        fut = asyncio.ensure_future(
            self._repo.finish_task(
                task_id, task_status,
                result=result, error=error, step=step,
            )
        )
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            # 外层被取消（用户点了取消 / 进程在关）：内层写入仍在跑，
            # 这里尽量把它等完，保证 DB 里不留「永远 running」的脏行。
            try:
                await fut
            except asyncio.CancelledError:
                # 取消持续打进来，实在写不完也只能放弃（已有 recover_interrupted 兜底）
                logger.warning(
                    "TaskService._finalize gave up: task_id=%s status=%s",
                    task_id, task_status,
                )
            except Exception:
                logger.exception(
                    "TaskService._finalize write FAILED after cancel: task_id=%s",
                    task_id,
                )
            # 关键：不把 CancelledError 继续往外抛。
            # 抛出去了 _run 的 except 分支就再也发不出 task.cancelled 事件，
            # 前端会一直转圈 —— 终态已经尽力写过，这里安静收尾即可。
        except Exception:
            logger.exception(
                "TaskService._finalize write FAILED: task_id=%s", task_id,
            )

    # ----------------------------------------------------------------
    # 事件发布
    # ----------------------------------------------------------------

    @staticmethod
    def _publish(conversation_id: str, event_type: str, data: dict) -> None:
        task_event_bus.publish(conversation_id, event_type, data)

    # ----------------------------------------------------------------
    # 结果写回会话的文本格式化
    # ----------------------------------------------------------------

    @staticmethod
    def _format_result(title: str, goal: str, result: dict) -> str:
        if not isinstance(result, dict):
            return f"【任务完成】{title}"

        # research worker 产出的是完整 Markdown 报告，直接整篇写回，
        # 不要再套一层「步骤：」列表（那样报告会变成一堆短句）。
        report = result.get("report")
        if report:
            header = f"【任务完成】{title}"
            if goal:
                header += f"\n\n> 目标：{goal}"
            return f"{header}\n\n{report}"

        summary = result.get("summary")
        steps = result.get("steps")
        lines = [f"【任务完成】{title}"]
        if goal:
            lines.append(f"目标：{goal}")
        if summary:
            lines.append(f"结论：{summary}")
        if steps:
            lines.append("步骤：")
            for i, s in enumerate(steps, 1):
                lines.append(f"  {i}. {s}")
        return "\n".join(lines)


# 模块级单例
task_service = TaskService()
