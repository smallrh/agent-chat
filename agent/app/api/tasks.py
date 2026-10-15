import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.auth import get_current_user_id
from app.repo.task_repo import TaskRepository
from app.service.task_events import task_event_bus
from app.service.task_service import TaskLimitReached, task_service
from app.utils.sse import sse


router = APIRouter(prefix="/tasks", tags=["Tasks"])


def _repo() -> TaskRepository:
    """
    统一取 TaskService 内部持有的 TaskRepository（复用同一个连接池）。

    背景：先前这里用的是 app.repo.task_repo 的模块级单例，而 lifespan 只调用
    task_service.start()（它启动的是 TaskService 自建的另一个实例），模块级
    单例从未启动 → 池为 None → /tasks 全部端点抛 "PostgreSQL pool is not started"。
    现在全链路只有一个 TaskRepository，也就只有一份连接池。
    """
    if not task_service.is_ready:
        # 通常意味着 lifespan STEP 6 启动失败（异常被隔离）或已 shutdown。
        # 给出明确的 503，而不是让底层抛 RuntimeError 变成无信息量的 500。
        raise HTTPException(
            status_code=503, detail="Task service is not available",
        )
    return task_service.repo


# ----------------------------------------------------------------
# 请求 / 响应模型
# ----------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    conversation_id: str
    title: str = Field(..., min_length=1, max_length=255)
    goal: str = ""
    kind: str = "demo"


class CreateTaskResponse(BaseModel):
    task_id: str
    status: str = "pending"


class TaskItem(BaseModel):
    task_id: str
    conversation_id: str
    user_id: int
    title: str | None = None
    goal: str | None = None
    # 任务类型（demo / research …），与 TaskService 注册的 handler 一一对应
    kind: str = "demo"
    status: str
    progress: int
    # 当前步骤文案（如「检索资料（2/3）：广州天河酒店」）
    step: str | None = None
    steps_done: int = 0
    steps_total: int = 0
    result: Any | None = None
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class TaskListResponse(BaseModel):
    items: list[TaskItem]


class TaskKindsResponse(BaseModel):
    items: list[str]


# ----------------------------------------------------------------
# 创建并后台运行
# ----------------------------------------------------------------

@router.get("/kinds", response_model=TaskKindsResponse)
async def list_kinds(_user_id: int = Depends(get_current_user_id)):
    """
    已注册的任务类型（前端渲染下拉框/图标用）。

    放在 /tasks/{task_id} **之前**声明，否则 "/kinds" 会被后面的
    /{task_id} 路由吃掉（FastAPI 按声明顺序匹配）。
    """
    return TaskKindsResponse(items=task_service.kinds)


@router.post("", response_model=CreateTaskResponse)
async def create_task(
    body: CreateTaskRequest,
    user_id: int = Depends(get_current_user_id),
):
    """创建任务并立即后台运行。返回 task_id 后接口立即返回，不阻塞。"""
    # 归属校验（也确保 conversation 存在且属于该用户）
    await _repo().assert_conversation_owner(body.conversation_id, user_id)
    try:
        task_id = await task_service.spawn_task(
            conversation_id=body.conversation_id,
            user_id=user_id,
            title=body.title,
            goal=body.goal,
            kind=body.kind,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TaskLimitReached as e:
        # 429：不是错误，是「稍后再试」。前端据此提示而不是报红。
        raise HTTPException(status_code=429, detail=str(e))
    return CreateTaskResponse(task_id=task_id)


# ----------------------------------------------------------------
# 列表 / 详情
# ----------------------------------------------------------------

@router.get("", response_model=TaskListResponse)
async def list_tasks(
    conversation_id: str | None = Query(
        None,
        description="会话 ID；不传 = 该用户全部会话的任务（任务中心页）",
    ),
    status_filter: str | None = Query(None, alias="status"),
    kind: str | None = Query(None, description="按任务类型筛选"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: int = Depends(get_current_user_id),
):
    """
    列出任务。带 conversation_id = 会话内面板；不带 = 任务中心（跨会话）。
    """
    if conversation_id:
        await _repo().assert_conversation_owner(conversation_id, user_id)
        rows = await _repo().list_tasks(
            conversation_id=conversation_id,
            user_id=user_id,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
        )
    else:
        rows = await _repo().list_user_tasks(
            user_id=user_id,
            status_filter=status_filter,
            kind=kind,
            limit=limit,
            offset=offset,
        )
    return TaskListResponse(items=[TaskItem(**r) for r in rows])


@router.get("/{task_id}", response_model=TaskItem)
async def get_task(
    task_id: str,
    user_id: int = Depends(get_current_user_id),
):
    row = await _repo().get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if int(row["user_id"]) != user_id:
        raise HTTPException(
            status_code=403, detail="Task does not belong to current user",
        )
    return TaskItem(**row)


# ----------------------------------------------------------------
# 取消
# ----------------------------------------------------------------

@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    user_id: int = Depends(get_current_user_id),
):
    ok = await task_service.cancel_task(task_id, user_id)
    if not ok:
        row = await _repo().get_task(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"ok": True, "cancelled": False, "reason": "already terminal"}
    return {"ok": True, "cancelled": True}


# ----------------------------------------------------------------
# 重跑（终态任务：failed / cancelled / 重启中断）
# ----------------------------------------------------------------

@router.post("/{task_id}/retry")
async def retry_task(
    task_id: str,
    user_id: int = Depends(get_current_user_id),
):
    """
    重跑一个已终态的任务，task_id 不变（前端卡片不用换 key）。

    典型场景：进程重启被标成 interrupted、或检索服务抖动导致整条失败。
    """
    try:
        ok = await task_service.retry_task(task_id, user_id)
    except TaskLimitReached as e:
        raise HTTPException(status_code=429, detail=str(e))
    if not ok:
        row = await _repo().get_task(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"ok": False, "retried": False, "reason": "still running or unknown kind"}
    return {"ok": True, "retried": True}


# ----------------------------------------------------------------
# SSE：实时进度流（订阅某会话下的所有任务事件）
# ----------------------------------------------------------------

@router.get("/stream")
async def task_stream(
    conversation_id: str = Query(..., description="会话 ID"),
    user_id: int = Depends(get_current_user_id),
):
    """
    订阅某会话的任务事件流（task.created / running / progress / completed /
    failed / cancelled）。前端常驻一条 SSE 连接即可收到所有后台任务进度。

    注意：Go BFF 需像代理 /chat/stream 那样透明透传此 SSE（不包业务信封）。
    """
    # 先校验归属（只读校验，不会像 ensure_owner 那样自动建会话）
    await _repo().assert_conversation_owner(conversation_id, user_id)

    q = task_event_bus.subscribe(conversation_id)

    async def event_gen():
        try:
            yield sse("task.stream.start", {"conversation_id": conversation_id})
            while True:
                try:
                    # 30s 心跳：长任务可能几分钟没有任何事件，中间的反向代理
                    # （nginx / Go / 浏览器）会按空闲超时把连接掐掉。
                    # 注释行 ": ping" 是 SSE 标准规定的心跳，前端解析时会跳过。
                    event = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield sse(event["type"], event["data"])
        finally:
            task_event_bus.unsubscribe(conversation_id, q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
