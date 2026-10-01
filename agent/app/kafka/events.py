from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class MemoryEvent(BaseModel):
    event_id:str
    event_type:str
    user_id:int
    conversation_id:str
    message_id:str
    role:str
    content:str
    timestamp: datetime=Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metadata: dict[str, Any] = Field(default_factory=dict)