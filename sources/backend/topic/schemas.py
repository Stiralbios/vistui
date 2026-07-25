import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class TopicBase(BaseModel):
    full_text: str = Field(max_length=32000)


class TopicRead(TopicBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chatgroup_id: uuid.UUID
    embedding: list[float] | None = None
    salience_score: float | None
    messages_linked_count: int = 0
    start_at: dt.datetime
    last_edited_at: dt.datetime
    created_at: dt.datetime
    updated_at: dt.datetime | None

    @model_validator(mode="before")
    @classmethod
    def inject_messages_linked_count(cls, data: Any, info: ValidationInfo) -> Any:
        if isinstance(data, dict) and "messages_linked_count" not in data:
            context = info.context or {}
            data["messages_linked_count"] = context.get("messages_linked_count", 0)
        return data


class TopicInternal(TopicRead):
    pass


class TopicCreate(TopicBase):
    start_at: dt.datetime
    last_edited_at: dt.datetime


class TopicContext(BaseModel):
    user_id: uuid.UUID


class TopicCreateInternal(TopicCreate):
    model_config = ConfigDict(extra="forbid")

    chatgroup_id: uuid.UUID
    context: TopicContext


class TopicUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_text: str | None = Field(default=None, max_length=32000)
    last_edited_at: dt.datetime | None = None

    @field_validator("full_text", "last_edited_at", mode="before")
    def prevent_explicit_none(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Explicit None is not allowed for this field")
        return value


class TopicUpdateInternal(TopicUpdate):
    id: uuid.UUID
    context: TopicContext


class TopicFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
