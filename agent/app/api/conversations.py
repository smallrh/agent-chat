from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.auth import get_current_user_id
from app.service.conversation_service import conversation_service

router = APIRouter(prefix="/conversations", tags=["Conversations"])


class ConversationItem(BaseModel):
    conversation_id: str
    title: str
    status: int
    last_message_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ConversationListResponse(BaseModel):
    items: list[ConversationItem]


class MessageItem(BaseModel):
    message_id: str
    role: str
    content: str
    sequence_id: int
    created_at: str | None = None
    metadata: dict | None = None


class MessageListResponse(BaseModel):
    items: list[MessageItem]


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    user_id: int = Depends(get_current_user_id),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    items = await conversation_service.list_conversations(user_id, limit=limit, offset=offset)
    return ConversationListResponse(items=[ConversationItem(**x) for x in items])


@router.get("/{conversation_id}/messages", response_model=MessageListResponse)
async def list_messages(
    conversation_id: str,
    user_id: int = Depends(get_current_user_id),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    items = await conversation_service.get_messages(
        conversation_id, user_id, limit=limit, offset=offset,
    )
    return MessageListResponse(items=[MessageItem(**x) for x in items])


@router.patch("/{conversation_id}", response_model=ConversationItem)
async def rename_conversation(
    conversation_id: str,
    body: RenameRequest,
    user_id: int = Depends(get_current_user_id),
):
    item = await conversation_service.rename(conversation_id, user_id, body.title.strip())
    return ConversationItem(**item)


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user_id: int = Depends(get_current_user_id),
):
    await conversation_service.delete(conversation_id, user_id)
    return {"ok": True}
