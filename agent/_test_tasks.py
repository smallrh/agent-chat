"""
P2 多任务 离线测试（无需 PG / Kafka / LLM）。

用 FakeRepo / FakeBus / FakeMessageService 替换真实依赖，验证核心链路：
  1. spawn_task → task.created
  2. 后台 _run → running → 逐步 progress（入库 + 发事件）
  3. 成功 → finish(succeeded) + 后台写回会话消息（create_assistant_message 被调用）
          + task.completed
  4. cancel：进行中可取消；终态不可取消
  5. list_tasks：能按会话列出

直接运行： python _test_tasks.py
"""
import asyncio
import sys

# ---- 用 fake 替换真实依赖（在导入 task_service 之前打补丁）----


class FakeRepo:
    def __init__(self):
        self.tasks = {}
        self._seq = 0
        self.write_backs = []  # (user_id, conversation_id, content)

    async def start(self):
        pass

    async def stop(self):
        pass

    async def assert_conversation_owner(self, conversation_id, user_id):
        pass  # 测试里不校验

    async def create_task(self, task_id, conversation_id, user_id, title, goal):
        self._seq += 1
        self.tasks[task_id] = {
            "id": self._seq, "task_id": task_id,
            "conversation_id": conversation_id, "user_id": user_id,
            "title": title, "goal": goal, "status": "pending",
            "progress": 0, "result": None, "error": None,
        }
        return task_id

    async def set_running(self, task_id):
        self.tasks[task_id]["status"] = "running"

    async def update_progress(self, task_id, progress):
        self.tasks[task_id]["progress"] = progress

    async def finish_task(self, task_id, task_status, result=None, error=None):
        self.tasks[task_id]["status"] = task_status
        self.tasks[task_id]["result"] = result
        if task_status == "succeeded":
            self.tasks[task_id]["progress"] = 100
        if error:
            self.tasks[task_id]["error"] = error

    async def cancel(self, task_id, user_id):
        st = self.tasks[task_id]["status"]
        if st in ("pending", "running"):
            self.tasks[task_id]["status"] = "cancelled"
            return True
        return False

    async def get_task(self, task_id):
        return self.tasks.get(task_id)

    async def list_tasks(self, conversation_id, user_id=None, status_filter=None,
                         limit=50, offset=0):
        rows = [t for t in self.tasks.values()
                if t["conversation_id"] == conversation_id]
        if status_filter:
            rows = [t for t in rows if t["status"] == status_filter]
        return rows


class FakeBus:
    def __init__(self):
        self.events = []

    def subscribe(self, conversation_id):
        return None

    def unsubscribe(self, conversation_id, q):
        pass

    def publish(self, conversation_id, event_type, data):
        self.events.append((conversation_id, event_type, data))


class FakeMessageService:
    def __init__(self, write_backs):
        self._write_backs = write_backs

    async def create_user_message(self, user_id, conversation_id, content):
        return "u-msg"

    async def create_assistant_message(self, user_id, conversation_id, content):
        self._write_backs.append((user_id, conversation_id, content))
        return "a-msg"


# ---- 注入 fake ----
import app.service.task_service as ts_module  # noqa: E402

fake_repo = FakeRepo()
fake_bus = FakeBus()
write_backs = []

ts_module.task_service._repo = fake_repo
ts_module.task_event_bus = fake_bus
ts_module.message_service = FakeMessageService(write_backs)

task_service = ts_module.task_service


async def test_full_flow():
    conv = "11111111-1111-1111-1111-111111111111"
    uid = 1

    # 1) spawn
    task_id = await task_service.spawn_task(conv, uid, "写季度报告", "整理 Q3 数据", "demo")
    assert task_id, "spawn 应返回 task_id"
    assert any(e[1] == "task.created" for e in fake_bus.events), "应发布 task.created"

    # 2) 等待后台 _run 跑完（demo 约 0.3*5 = 1.5s）
    await asyncio.sleep(2.2)

    row = await fake_repo.get_task(task_id)
    assert row["status"] == "succeeded", f"应成功，实际 {row['status']}"
    assert row["progress"] == 100, f"进度应 100，实际 {row['progress']}"
    assert any(e[1] == "task.completed" for e in fake_bus.events), "应发布 task.completed"

    # 3) 后台写回：应有一条约等于会话结果消息
    assert write_backs, "应有后台写回消息"
    wb = write_backs[0]
    assert wb[0] == uid and wb[1] == conv, f"写回归属错误: {wb}"

    # 4) progress 事件应逐步递增，最终到 100
    progress_events = [e for e in fake_bus.events if e[1] == "task.progress"]
    assert progress_events, "应有 task.progress 事件"
    last_pct = progress_events[-1][2]["progress"]
    assert last_pct == 100, f"最后进度应 100，实际 {last_pct}"

    # 5) list
    rows = await fake_repo.list_tasks(conv, user_id=uid)
    assert len(rows) == 1 and rows[0]["task_id"] == task_id

    print(f"[OK] full flow: task_id={task_id} status={row['status']} "
          f"progress={row['progress']} writebacks={len(write_backs)}")


async def test_cancel_running():
    conv = "22222222-2222-2222-2222-222222222222"
    uid = 1
    task_id = await task_service.spawn_task(conv, uid, "长跑任务", "", "demo")

    # 立刻取消（还在 running / pending 阶段）
    ok = await task_service.cancel_task(task_id, uid)
    assert ok is True, "进行中任务应可取消"

    await asyncio.sleep(0.2)
    row = await fake_repo.get_task(task_id)
    assert row["status"] == "cancelled", f"应 cancelled，实际 {row['status']}"

    # 终态再取消：应返回 False
    ok2 = await task_service.cancel_task(task_id, uid)
    assert ok2 is False, "终态任务不应再取消成功"

    print(f"[OK] cancel: task_id={task_id} status={row['status']} re-cancel={ok2}")


async def main():
    print("=== P2 多任务离线测试 ===")
    await test_full_flow()
    await test_cancel_running()
    print("=== 全部通过 ✅ ===")


if __name__ == "__main__":
    asyncio.run(main())
