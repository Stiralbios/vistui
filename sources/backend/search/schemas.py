"""Pydantic schemas for the search API."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    type: str = Field(description="Memory type: message, event, topic or fact.")
    id: uuid.UUID
    text: str
    score: float = Field(description="Final score after applying the chatgroup's ranking formula.")
    vector_score: float = Field(
        description="Cosine similarity between the query and the item embedding, clamped to 0..1 (0 when absent)."
    )
    bm25_score: float = Field(
        description="ParadeDB BM25 relevance normalized to 0..1 against the query's best lexical hit (0 when no match)."
    )
    salience: float = Field(description="Salience used by the formula (1.0 when unset or disabled).")
    linked_count: int = Field(description="Number of memory objects linked to this item.")
    tokens: int = Field(
        description="BPE token estimate of the hit's text (whitespace words when the tokenizer is "
        "unavailable); the packing budget counts these."
    )


class SearchResponse(BaseModel):
    q: str
    hits: list[SearchHit]
    token_budget: int = Field(description="Token budget the hits were packed into.")
    tokens_used: dict[str, int] = Field(
        description="Token estimates of the returned hits, per memory type (message, event, topic, fact)."
    )
    memory_ratio: dict[str, float] = Field(
        description="Share of the budget per memory type; sums to 1 for the decision source."
    )
    ratio_source: Literal["config", "decision"] = Field(
        description="Where the ratio came from: the chatgroup config or the decision model."
    )
