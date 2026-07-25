import datetime as dt
import uuid
from typing import Any

from backend.message.constants import MessageRole, ProcessingStatus
from pydantic import BaseModel, ConfigDict, Field, field_validator


def default_processing_state() -> dict:
    return {
        "embedding": ProcessingStatus.WAITING,
        "salience": ProcessingStatus.WAITING,
        "event": ProcessingStatus.WAITING,
        "topic": ProcessingStatus.WAITING,
        "fact": ProcessingStatus.WAITING,
        "last_failure_at": None,
        "retry_count": 0,
    }


class MessageBase(BaseModel):
    full_text: str = Field(max_length=32000)
    role: MessageRole


class MessageRead(MessageBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chat_id: uuid.UUID
    prev_message_id: uuid.UUID | None
    salience_score: float | None
    processing_state: dict
    created_at: dt.datetime
    updated_at: dt.datetime | None


class MessageInternal(MessageRead):
    pass


class MessageCreate(MessageBase):
    model_config = ConfigDict(extra="forbid")


class MessageContext(BaseModel):
    user_id: uuid.UUID


class MessageCreateInternal(MessageCreate):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    chat_id: uuid.UUID
    prev_message_id: uuid.UUID | None
    processing_state: dict
    context: MessageContext


class MessageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_text: str | None = Field(default=None, max_length=32000)
    role: MessageRole | None = None

    @field_validator("full_text", "role", mode="before")
    def prevent_explicit_none(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Explicit None is not allowed for this field")
        return value


class MessageUpdateInternal(MessageUpdate):
    id: uuid.UUID
    chat_id: uuid.UUID
    context: MessageContext


class MessageFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
    role: MessageRole | None = None
