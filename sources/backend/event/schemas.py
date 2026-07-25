import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class EventBase(BaseModel):
    full_text: str = Field(max_length=32000)
    start_at: dt.datetime
    end_at: dt.datetime


class EventRead(EventBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chat_id: uuid.UUID
    embedding: list[float] | None = None
    salience_score: float | None
    messages_linked_count: int = 0
    created_at: dt.datetime
    updated_at: dt.datetime | None

    @model_validator(mode="before")
    @classmethod
    def inject_messages_linked_count(cls, data: Any, info: ValidationInfo) -> Any:
        if isinstance(data, dict) and "messages_linked_count" not in data:
            context = info.context or {}
            data["messages_linked_count"] = context.get("messages_linked_count", 0)
        return data


class EventInternal(EventRead):
    pass


class EventCreate(EventBase):
    pass


class EventContext(BaseModel):
    user_id: uuid.UUID


class EventCreateInternal(EventCreate):
    model_config = ConfigDict(extra="forbid")

    chat_id: uuid.UUID
    context: EventContext


class EventUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_text: str | None = Field(default=None, max_length=32000)
    start_at: dt.datetime | None = None
    end_at: dt.datetime | None = None

    @field_validator("full_text", "start_at", "end_at", mode="before")
    def prevent_explicit_none(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Explicit None is not allowed for this field")
        return value


class EventUpdateInternal(EventUpdate):
    id: uuid.UUID
    context: EventContext


class EventFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
