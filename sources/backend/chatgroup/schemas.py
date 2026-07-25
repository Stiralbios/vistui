import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_RANKING_FORMULA = (
    "lambda vector_score, bm25_score, salience, linked_count: "
    "max(vector_score * salience * linked_count, bm25_score * salience * linked_count)"
)


class MemoryRatio(BaseModel):
    message: float = 0.40
    event: float = 0.10
    topic: float = 0.30
    fact: float = 0.20


class ChatGroupConfig(BaseModel):
    token_budget: int = 2000
    memory_ratio: MemoryRatio = Field(default_factory=MemoryRatio)
    use_llm_memory_ratio: bool = False
    use_salience: bool = True
    protected_message_count: int = 10
    topic_update_threshold: float = 0.75
    fact_update_threshold: float = 0.80
    fact_history_count: int = 5
    max_facts_per_message: int = 5
    max_topics_per_message: int = 5
    batch_inactivity_seconds: int = 3600
    processing_timeout_seconds: int = 3600


class ChatGroupPrompts(BaseModel):
    salience: str = "Assign a salience score between 0 and 1 to the message."
    keywords: str = "Extract a list of search keywords from the message."
    memory_ratio: str = "Determine the desired memory category distribution."
    event: str = "Summarize a contiguous block of messages into one or more events."
    topic_candidate: str = "Select existing topics to update or decide to create a new topic."
    topic_update: str = "Update a specific topic given the message and ChatGroup context."
    fact_candidate: str = "Select existing facts to update or decide to create a new fact."
    fact_update: str = "Update a specific fact given the message and its recent history."


class ChatGroupBase(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=1024)


class ChatGroupRead(ChatGroupBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    config: ChatGroupConfig
    prompts: ChatGroupPrompts
    ranking_formula: str
    created_at: dt.datetime
    updated_at: dt.datetime | None


class ChatGroupInternal(ChatGroupRead):
    pass


class ChatGroupCreate(ChatGroupBase):
    config: ChatGroupConfig = Field(default_factory=ChatGroupConfig)
    prompts: ChatGroupPrompts = Field(default_factory=ChatGroupPrompts)
    ranking_formula: str = DEFAULT_RANKING_FORMULA


class ChatGroupContext(BaseModel):
    user_id: uuid.UUID


class ChatGroupCreateInternal(ChatGroupCreate):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    context: ChatGroupContext


class ChatGroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    config: ChatGroupConfig | None = None
    prompts: ChatGroupPrompts | None = None
    ranking_formula: str | None = None

    @field_validator("name", "description", "ranking_formula", mode="before")
    def prevent_explicit_none(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Explicit None is not allowed for this field")
        return value


class ChatGroupUpdateInternal(ChatGroupUpdate):
    id: uuid.UUID
    context: ChatGroupContext


class ChatGroupFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
