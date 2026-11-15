import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Ranking default: both score channels are normalized to 0..1 by `backend.search.scoring`
# before the formula runs, and `link_weight` gives an unconsolidated item 0.5 and a
# 100-link item 1.0 (logarithmic in between). Formulas are user-authored and evaluated
# with `eval`; the resulting exposure is tracked as issue 001 in `plan/issue.md`.
DEFAULT_RANKING_FORMULA = (
    "lambda vector_score, bm25_score, salience, linked_count: "
    "max(vector_score, bm25_score) * salience * link_weight(linked_count)"
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
    # Null or false opts the chatgroup out of salience scoring: rows keep
    # salience_score NULL (nothing is fabricated) and the search formula passes the
    # neutral salience (1.0) for them.
    use_salience: bool | None = True
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
    memory_ratio: str = "Determine the desired memory category distribution."
    event: str = "Summarize a contiguous block of messages into one or more events."
    topic_candidate: str = "Select existing topics to update or decide to create a new topic."
    topic_update: str = "Update a specific topic given the message and ChatGroup context."
    fact_candidate: str = "Select existing facts to update or decide to create a new fact."
    fact_update: str = "Update a specific fact given the message and its recent history."


class ChatGroupBase(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    # Pydantic-level default shared by every create schema (ChatGroupCreate and its
    # Internal variant); the column itself has no server default on purpose.
    ranking_formula: str = DEFAULT_RANKING_FORMULA


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
