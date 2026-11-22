import datetime as dt
import uuid
from typing import Any

from backend.llm.prompts import (
    DEFAULT_EVENT_BOUNDARY_CRITERIA,
    DEFAULT_MEMORY_RATIO_CRITERIA,
    DEFAULT_SALIENCE_RUBRIC,
    EVENT_BOUNDARY,
    EVENT_SUMMARY,
    FACT_CANDIDATE,
    MEMORY_RATIO,
    SALIENCE,
    TOPIC_CANDIDATE,
)
from backend.search.formulas import DEFAULT_RANKING_FORMULA_NAME, get_ranking_formulas
from backend.settings import AppSettings
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    use_salience: bool = True
    # The x most recent messages of a chat never enter facts/events/topics until newer
    # messages push them out of the window. The default follows the deployment's
    # VISTUI_PROTECTED_MESSAGE_COUNT setting; each chatgroup can override it.
    protected_message_count: int = Field(default_factory=lambda: AppSettings().VISTUI_PROTECTED_MESSAGE_COUNT)
    max_facts_per_message: int = 5
    max_topics_per_message: int = 5
    batch_inactivity_seconds: int = 3600
    processing_timeout_seconds: int = 3600


class ChatGroupPrompts(BaseModel):
    # All default texts live in backend.llm.prompts (one source of truth shared with
    # the pipelines); the comments there describe each prompt.
    salience: str = SALIENCE
    # Ordered rubric levels offered to the decision model, lowest first. The decision
    # model scores against 2-10 levels, so shorter or longer rubrics are rejected.
    salience_rubric: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SALIENCE_RUBRIC), min_length=2, max_length=10
    )
    memory_ratio: str = MEMORY_RATIO
    # Criteria of the memory-ratio choice question, keyed by memory type (at least
    # two entries — choice questions need two or more options).
    memory_ratio_criteria: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_MEMORY_RATIO_CRITERIA))
    # Instructions of the event-boundary choice question: how the next message relates
    # to the chat's last event (the state carries both, the criteria carry the three
    # actions).
    event_boundary: str = EVENT_BOUNDARY
    # Criteria of the event-boundary choice question, keyed by action: extend/new/mixed.
    # Subsets are allowed (at least two options), so a chatgroup can forbid an action.
    event_boundary_criteria: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_EVENT_BOUNDARY_CRITERIA))
    # Generation template for event summaries: `{messages}` is substituted with the
    # event's full message list.
    event_summary: str = EVENT_SUMMARY
    topic_candidate: str = TOPIC_CANDIDATE
    fact_candidate: str = FACT_CANDIDATE

    @field_validator("memory_ratio_criteria")
    @classmethod
    def validate_memory_ratio_criteria(cls, value: dict[str, str]) -> dict[str, str]:
        """The criteria keys must be memory types — the decision model's probability
        keys and the packing shares are looked up by them — and choice questions need
        at least two options."""
        unknown = set(value) - set(DEFAULT_MEMORY_RATIO_CRITERIA)
        if unknown:
            raise ValueError(f"memory_ratio_criteria keys must be memory types, got: {sorted(unknown)}")
        if len(value) < 2:
            raise ValueError("memory_ratio_criteria needs at least two entries")
        return value

    @field_validator("event_boundary_criteria")
    @classmethod
    def validate_event_boundary_criteria(cls, value: dict[str, str]) -> dict[str, str]:
        """The criteria keys must be the pipeline's actions — the event pipeline switches
        on them — and choice questions need at least two options."""
        unknown = set(value) - set(DEFAULT_EVENT_BOUNDARY_CRITERIA)
        if unknown:
            raise ValueError(f"event_boundary_criteria keys must be event actions, got: {sorted(unknown)}")
        if len(value) < 2:
            raise ValueError("event_boundary_criteria needs at least two entries")
        return value

    @field_validator("event_summary")
    @classmethod
    def validate_event_summary(cls, value: str) -> str:
        """The template is formatted with the event's message list: without the
        `{messages}` placeholder the summary would be built from nothing, and an
        otherwise-unformattable template (stray braces) would fail every message."""
        if "{messages}" not in value:
            raise ValueError("event_summary must contain the {messages} placeholder")
        try:
            value.format(messages="sample")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"event_summary is not a valid format template: {exc}") from exc
        return value


class ChatGroupBase(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    # Name of a formula registered in the settings file's ranking_formulas section.
    # The write schemas validate it against the registry — the API accepts names
    # only, never formula source. Pydantic-level default shared by every create
    # schema (ChatGroupCreate and its Internal variant); the column itself has no
    # server default on purpose.
    ranking_formula: str = DEFAULT_RANKING_FORMULA_NAME


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

    @field_validator("ranking_formula")
    @classmethod
    def validate_ranking_formula(cls, value: str) -> str:
        """Only names registered in the settings file are accepted."""
        if value not in get_ranking_formulas():
            raise ValueError(f"unknown ranking formula name: {value}")
        return value


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

    @field_validator("ranking_formula")
    @classmethod
    def validate_ranking_formula(cls, value: str) -> str:
        """Only names registered in the settings file are accepted."""
        if value not in get_ranking_formulas():
            raise ValueError(f"unknown ranking formula name: {value}")
        return value


class ChatGroupUpdateInternal(ChatGroupUpdate):
    id: uuid.UUID
    context: ChatGroupContext


class ChatGroupFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
