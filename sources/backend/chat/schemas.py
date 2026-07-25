import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatBase(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=1024)


class ChatRead(ChatBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chatgroup_id: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime | None


class ChatInternal(ChatRead):
    pass


class ChatCreate(ChatBase):
    chatgroup_id: uuid.UUID | None = None


class ChatContext(BaseModel):
    user_id: uuid.UUID


class ChatCreateInternal(ChatCreate):
    model_config = ConfigDict(extra="forbid")
    context: ChatContext


class ChatUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1024)

    @field_validator("name", "description", mode="before")
    def prevent_explicit_none(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Explicit None is not allowed for this field")
        return value


class ChatUpdateInternal(ChatUpdate):
    id: uuid.UUID
    context: ChatContext


class ChatFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
