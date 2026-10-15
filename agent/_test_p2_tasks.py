"""
P2 真任务队列 —— 端到端自检（不依赖 PG / Redis / 外网）。

用假 repo 替掉 TaskRepository，只验证 TaskService 的调度逻辑：
  spawn → running → progress → succeeded / cancelled / failed
  并发上限、协作式取消、终态不被覆盖

运行：
    .venv/Scripts/python.exe _test_p2_tasks.py
"""

import asyncio
import sys

sys.path.insert(0, ".")

from app.service import task_service as ts_mod
from app.service.task_service import TaskLimitReached, task_service
from app.service.task_events import task_event_bus
from app.service.task_worker import TaskCancelToken, TaskCancelled


class FakeRepo:
    def __init__(self):
        self.pool = object()  # 假装已启动
        self.rows: dict[str, dict] = {}
        self.events_ignored = 0

    async def create_task(self, task_id, conversation_id, user_id, title, goal, kind="demo"):
        self.rows[task_id] = {
            "task_id": task_id, "conversation_id": conversation_id,
            "user_id": user_id, "title": title, "goal": goal, "kind": kind,
            "status": "pending", "progress": 0, "step": "",
            "steps_done": 0, "steps_total": 0, "result": None, "error": None,
        }
        return task_id

    async def set_running(self, task_id, steps_total=0):
        self.rows[task_id]["status"] = "running"

    async def update_progress(self, task_id, progress, step=None, steps_done=None, steps_total=None):
        r = self.rows[task_id]
        r["progress"] = progress
        if step is not None:
            r["step"] = step
        if steps_done is not None:
            r["steps_done"] = steps_done
        if steps_total is not None:
            r["steps_total"] = steps_total

    async def finish_task(self, task_id, task_status, result=None, error=None, step=None):
        r = self.rows[task_id]
        r["status"] = task_status
        if result is not None:
            r["result"] = result
        if error is not None:
            r["error"] = error
        if task_status == "succeeded":
            r["progress"] = 100
        if step is not None:
            r["step"] = step

    async def get_task(self, task_id):
        return self.rows.get(task_id)

    async def cancel(self, task_id, user_id):
        r = self.rows.get(task_id)
        if not r or r["status"] not in ("pending", "running"):
            return False
        r["status"] = "cancelled"
        return True

    async def count_active(self, user_id=None):
        n = 0
        for r in self.rows.values():
            if r["status"] in ("pending", "running"):
                if user_id is None or r["user_id"] == user_id:
                    n += 1
        return n

    async def recover_interrupted(self, limit=500):
        return []


class FakeMessageService:
    def __init__(self):
        self.written: list[str] = []

    async def create_assistant_message(self, user_id, conversation_id, content):
        self.written.append(content)
        return "msg-id"


CONV = "11111111-1111-1111-1111-111111111111"


def setup():
    repo = FakeRepo()
    task_service._repo = repo
    task_service._started = True
    fake_msgs = FakeMessageService()
    ts_mod.message_service = fake_msgs
    return repo, fake_msgs


def collect(conversation_id):
    """订阅该会话的事件，返回累积列表。"""
    q = task_event_bus.subscribe(conversation_id)
    seen: list[tuple[str, dict]] = []

    async def reader():
        while True:
            ev = await q.get()
            seen.append((ev["type"], ev["data"]))
            if ev["type"] in ("task.completed", "task.failed", "task.cancelled"):
                return

    return asyncio.create_task(reader()), seen, q


async def case_succeeded():
    print("\n=== 1) demo 任务正常跑完 ===")
    repo, msgs = setup()
    task, seen, q = collect(CONV)
    tid = await task_service.spawn_task(CONV, 1, "演示任务", "验证链路", kind="demo")
    await asyncio.wait_for(task, timeout=10)
    await task_service._running[tid] if tid in task_service._running else None
    row = repo.rows[tid]
    print("  终态:", row["status"], "进度:", row["progress"], "step:", row["step"])
    print("  步骤:", row["result"]["steps"][:2], "...")
    print("  写回消息:", (msgs.written[0].splitlines()[0] if msgs.written else "无"))
    print("  事件序列:", [t for t, _ in seen])
    task_event_bus.unsubscribe(CONV, q)
    assert row["status"] == "succeeded" and row["progress"] == 100
    assert msgs.written, "结果应写回会话"
    print("  ✅ 通过")


async def case_cancel():
    print("\n=== 2) 协作式取消（handler 在同步调用里也能停）===")
    repo, msgs = setup()

    async def slow_handler(*, task_id, progress, cancel_token: TaskCancelToken, **kw):
        await progress(1, 3, "第一步")
        # 模拟「卡在同步调用里」——cancel_token 是唯一能让它停下的手段
        for _ in range(100):
            cancel_token.raise_if_cancelled()
            await asyncio.sleep(0.05)
        return {"summary": "不该跑到这里"}

    task_service.register_handler("slow", slow_handler)
    task, seen, q = collect(CONV)
    tid = await task_service.spawn_task(CONV, 1, "慢任务", "", kind="slow")
    await asyncio.sleep(0.2)
    ok = await task_service.cancel_task(tid, 1)
    await asyncio.wait_for(task_service._running[tid], timeout=5) if tid in task_service._running else None
    await asyncio.sleep(0.1)
    row = repo.rows[tid]
    print("  cancel 返回:", ok, "终态:", row["status"], "step:", row["step"])
    print("  事件序列:", [t for t, _ in seen])
    task_event_bus.unsubscribe(CONV, q)
    assert ok is True
    assert row["status"] == "cancelled", row["status"]
    assert not msgs.written, "取消后不应写回结果消息"
    print("  ✅ 通过（终态写入没被取消打断）")


async def case_limit():
    print("\n=== 3) 单用户并发上限 ===")
    repo, _ = setup()
    ts_mod.settings.TASK_MAX_ACTIVE_PER_USER = 2

    async def hang(*, task_id, progress, cancel_token, **kw):
        await asyncio.sleep(10)
        return {"summary": "x"}

    task_service.register_handler("hang", hang)
    a = await task_service.spawn_task(CONV, 1, "任务A", "", kind="hang")
    b = await task_service.spawn_task(CONV, 1, "任务B", "", kind="hang")
    try:
        await task_service.spawn_task(CONV, 1, "任务C", "", kind="hang")
        print("  ❌ 第三个任务应当被拒绝")
        return
    except TaskLimitReached as e:
        print("  第三个被拒:", e)
    # 另一个用户不受影响
    d = await task_service.spawn_task(CONV, 2, "用户2的任务", "", kind="hang")
    print("  另一用户可建:", d[:8], "…")
    for t in list(task_service._running.values()):
        t.cancel()
    await asyncio.sleep(0.05)
    print("  ✅ 通过")


async def case_no_overwrite():
    print("\n=== 4) 竞态：handler 跑完前用户已取消 → 终态保持 cancelled ===")

    async def race(*, task_id, progress, cancel_token, **kw):
        await progress(1, 2, "干活")
        # 模拟：干活期间用户点了取消
        await task_service.cancel_task(task_id, 1)
        return {"summary": "我跑完了"}

    repo, msgs = setup()
    task_service.register_handler("race", race)
    tid = await task_service.spawn_task(CONV, 1, "竞态任务", "", kind="race")
    t = task_service._running[tid]
    await asyncio.wait_for(t, timeout=5)
    row = repo.rows[tid]
    print("  终态:", row["status"], "（不应是 succeeded）")
    print("  是否写回消息:", bool(msgs.written))
    assert row["status"] == "cancelled", row["status"]
    assert not msgs.written
    print("  ✅ 通过")


async def case_retry():
    print("\n=== 5) 重跑一个 failed 任务（task_id 不变）===")
    repo, msgs = setup()
    tid = await task_service.spawn_task(CONV, 1, "重跑任务", "goal", kind="demo")
    t = task_service._running[tid]
    await asyncio.wait_for(t, timeout=10)
    repo.rows[tid]["status"] = "failed"
    repo.rows[tid]["error"] = "interrupted by restart"
    ok = await task_service.retry_task(tid, 1)
    t2 = task_service._running[tid]
    await asyncio.wait_for(t2, timeout=10)
    row = repo.rows[tid]
    print("  retry 返回:", ok, "重跑后终态:", row["status"], "进度:", row["progress"])
    print("  结果:", row["result"]["summary"])
    assert ok and row["status"] == "succeeded"
    print("  ✅ 通过")


async def main():
    await case_succeeded()
    await case_cancel()
    await case_limit()
    await case_no_overwrite()
    await case_retry()
    print("\n全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
