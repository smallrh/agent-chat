from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.auth import get_current_user_id
from app.req.chat_req import ChatRequest
from app.rep.chat_rep import ChatResponse
from app.service.chat_service import chat_service

router = APIRouter(
    prefix="",
    tags=["Chat"],
)


@router.post(
    "/chat",
    response_model=ChatResponse,
)
async def chat(
    request: ChatRequest,
    user_id: int = Depends(get_current_user_id),
):
    """非流式聊天。

    user_id 由 FastAPI 依赖从 X-User-Id Header（Go Auth 注入的 user_uuid）
    解析得到，Python Agent 内部用的 users.id（int8）。
    """
    return await chat_service.chat(request, user_id=user_id)


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user_id: int = Depends(get_current_user_id),
):
    """SSE 流式聊天。

    先在 generator 外层完成 conversation ownership 校验（保证 HTTPException
    能被 FastAPI 正确转成 403 响应），再返回 StreamingResponse。
    """
    conversation_id = await chat_service.ensure_conversation_owner(
        request, user_id=user_id,
    )
    return StreamingResponse(
        chat_service.stream_chat(
            request,
            user_id=user_id,
            conversation_id=conversation_id,
        ),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class CancelRequest(BaseModel):
    run_id: str


@router.post("/chat/cancel")
async def chat_cancel(
    body: CancelRequest,
    user_id: int = Depends(get_current_user_id),
):
    """取消一个正在进行的流式生成（前端「停止生成」时调用）。

    通过 run_id 找到对应 RunToken 并置位取消标志；Agent 在生成过程中
    （每读一段 / 每个工具执行前）检查到该标志会抛 AgentCancelled 提前结束，
    随后走正常的尾部事件（message.end/agent.end/done）并落库部分内容。
    """
    from app.agent.cancel import get_token

    token = get_token(body.run_id)
    if token is None:
        # 已经结束 / 不存在：幂等返回成功，避免前端误报
        return {"ok": True, "cancelled": False}
    if token.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Run does not belong to current user.",
        )
    token.cancel()
    return {"ok": True, "cancelled": True}
