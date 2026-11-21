import datetime as dt
import uuid
from typing import Any

from backend.settings import AppSettings
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Ranking default: both score channels are normalized to 0..1 by `backend.search.scoring`
# before the formula runs, and `link_weight` gives an unconsolidated item 0.5 and a
# 100-link item 1.0 (logarithmic in between). Formulas are user-authored and evaluated
# with `eval`, which is not a security boundary (see `backend.search.scoring`).
DEFAULT_RANKING_FORMULA = (
    "lambda vector_score, bm25_score, salience, linked_count: "
    "max(vector_score, bm25_score) * salience * link_weight(linked_count)"
)

# Ordered default rubric for the salience decision question, lowest level first: the
# decision model's answer is a probability-weighted zero-based level index, normalized
# by len(rubric) - 1 into the 0..1 salience. Chatgroups override it per prompts key.
DEFAULT_SALIENCE_RUBRIC = [
    "Trivial: greetings, filler, or noise; not worth remembering.",
    "Low: minor context that will rarely matter later.",
    "Moderate: useful context or conversational continuity.",
    "High: meaningful personal facts, plans, or preferences worth keeping.",
    "Critical: core information - decisions, key people, dates, commitments.",
]

# Criteria of the memory-ratio choice question, keyed by memory type: the decision
# model reports its probability distribution over these keys, and the search packing
# looks its shares up by them — so the keys are fixed to the four memory types while
# chatgroups reword the descriptions. A type left out of the criteria is offered no
# share of the retrieval budget.
DEFAULT_MEMORY_RATIO_CRITERIA = {
    "message": "A verbatim chat message.",
    "event": "A summary of a contiguous sequence of messages.",
    "topic": "A summary of a recurring theme across the chatgroup.",
    "fact": "A small persistent fact about the user or the world state.",
}


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
    topic_update_threshold: float = 0.75
    fact_update_threshold: float = 0.80
    fact_history_count: int = 5
    max_facts_per_message: int = 5
    max_topics_per_message: int = 5
    batch_inactivity_seconds: int = 3600
    processing_timeout_seconds: int = 3600


class ChatGroupPrompts(BaseModel):
    # Full instruction template for the salience decision question: `{kind}` is
    # substituted with "message", "event", "topic" or "fact"; a template without the
    # placeholder is sent verbatim.
    salience: str = "Judge how much the {kind} in the state is worth remembering for future conversations."
    # Ordered rubric levels offered to the decision model, lowest first. The decision
    # model scores against 2-10 levels, so shorter or longer rubrics are rejected.
    salience_rubric: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SALIENCE_RUBRIC), min_length=2, max_length=10
    )
    memory_ratio: str = "Determine the desired memory category distribution."
    # Criteria of the memory-ratio choice question, keyed by memory type (at least
    # two entries — choice questions need two or more options).
    memory_ratio_criteria: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_MEMORY_RATIO_CRITERIA))
    event: str = "Summarize a contiguous block of messages into one or more events."
    topic_candidate: str = "Select existing topics to update or decide to create a new topic."
    topic_update: str = "Update a specific topic given the message and ChatGroup context."
    fact_candidate: str = "Select existing facts to update or decide to create a new fact."
    fact_update: str = "Update a specific fact given the message and its recent history."

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
