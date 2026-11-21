"""Unit tests for token-budget packing and memory-ratio normalization (no DB)."""

import uuid

import backend.search.budget as budget
import pytest
from backend.search.budget import MEMORY_TYPES, estimate_tokens, normalize_probabilities, pack_token_budget
from backend.search.schemas import SearchHit


def _hit(type_label: str, text: str, score: float, tokens: int | None = None) -> SearchHit:
    return SearchHit(
        type=type_label,
        id=uuid.uuid4(),
        text=text,
        score=score,
        vector_score=0.0,
        bm25_score=0.0,
        salience=1.0,
        linked_count=0,
        tokens=tokens if tokens is not None else estimate_tokens(text),
    )


def test_estimate_tokens_counts_bpe_pieces():
    """Token estimates use the local BPE tokenizer (tiktoken's cl100k_base — the same
    library LangChain's text splitters use): sub-word pieces and punctuation count,
    not just whitespace words. The counts below are cl100k_base exacts."""
    assert estimate_tokens("one two three") == 3  # simple words are single pieces
    assert estimate_tokens("rock-climbing, now!") == 7  # hyphen and punctuation split
    assert estimate_tokens("I went hiking in the mountains last weekend.") == 9
    assert estimate_tokens("") == 0


def test_estimate_tokens_falls_back_without_tokenizer(monkeypatch):
    """When the BPE tokenizer cannot load (package missing, or the encoding data is
    unreachable and not cached), estimation degrades to whitespace word counts
    instead of failing the search."""
    monkeypatch.setattr(budget, "_load_bpe_encoding", lambda: None)
    assert estimate_tokens("rock-climbing, now!") == 2
    assert estimate_tokens("  spaced   out  ") == 2
    assert estimate_tokens("word") == 1


def test_normalize_probabilities_renormalizes_and_fills_types():
    """The distribution always covers the four memory types: missing labels count as 0,
    unknown labels are dropped, negatives clamped, and the values renormalized to 1."""
    ratio = normalize_probabilities({"message": 4.0, "topic": 1.0})
    assert ratio == {"fact": 0.0, "topic": 0.2, "event": 0.0, "message": 0.8}

    ratio = normalize_probabilities({"message": 0.25, "event": 0.25, "gibberish": 5.0, "fact": -2.0})
    assert ratio == {"fact": 0.0, "topic": 0.0, "event": 0.5, "message": 0.5}


def test_normalize_probabilities_rejects_unusable_answers():
    """An absent, empty or all-zero probability set has no usable distribution."""
    assert normalize_probabilities({}) is None
    assert normalize_probabilities({"message": 0.0, "topic": 0.0}) is None
    assert normalize_probabilities({"gibberish": 1.0}) is None
    assert normalize_probabilities({"message": -1.0}) is None


def test_pack_respects_shares_in_score_order():
    """Each type takes its candidates in descending score order until one no longer
    fits its share; later (smaller) candidates of that type are not reconsidered."""
    hits = [
        _hit("fact", "best fact", 0.9, tokens=5),
        _hit("fact", "longer second fact", 0.8, tokens=16),
        _hit("fact", "tiny third fact", 0.7, tokens=1),
        _hit("topic", "topic hit", 0.6, tokens=2),
    ]
    ratio = {"fact": 0.2, "topic": 0.3, "event": 0.1, "message": 0.4}
    packed, tokens_used = pack_token_budget(hits, token_budget=100, memory_ratio=ratio, k=20)
    assert [hit.text for hit in packed] == ["best fact", "topic hit"]
    # Fact share is 20 tokens: the 5-token best fact fits, the 16-token second does not
    # and stops the type; the 1-token third fact is never reconsidered.
    assert tokens_used == {"fact": 5, "topic": 2, "event": 0, "message": 0}


def test_pack_includes_best_hit_even_over_share():
    """A type with a positive share always keeps its single best hit, even when that
    hit alone exceeds the share: a tiny budget must not silence a whole memory type."""
    hits = [
        _hit("fact", "a very long fact", 0.9, tokens=9),
        _hit("fact", "second fact", 0.8, tokens=2),
    ]
    packed, tokens_used = pack_token_budget(hits, token_budget=4, memory_ratio={"fact": 1.0}, k=20)
    assert [hit.text for hit in packed] == ["a very long fact"]
    assert tokens_used["fact"] == 9


def test_pack_skips_zero_share_types():
    """A type whose share is exactly zero yields nothing, even for its best hit."""
    hits = [
        _hit("event", "an event summary", 0.9, tokens=3),
        _hit("message", "a message", 0.8, tokens=2),
    ]
    packed, tokens_used = pack_token_budget(hits, token_budget=100, memory_ratio={"event": 0.0, "message": 1.0}, k=20)
    assert [hit.type for hit in packed] == ["message"]
    assert tokens_used == {"fact": 0, "topic": 0, "event": 0, "message": 2}


def test_pack_sorts_globally_and_caps_k():
    """The packed list is globally sorted by score (not grouped per type) and capped by
    k; tokens_used reports the returned hits only."""
    hits = [
        _hit("message", "m one", 0.9, tokens=2),
        _hit("fact", "f one", 0.8, tokens=2),
        _hit("topic", "t one", 0.7, tokens=2),
        _hit("event", "e one", 0.6, tokens=2),
        _hit("message", "m two", 0.5, tokens=2),
    ]
    packed, tokens_used = pack_token_budget(
        hits, token_budget=100, memory_ratio={"message": 0.4, "fact": 0.2, "topic": 0.3, "event": 0.1}, k=3
    )
    assert [hit.type for hit in packed] == ["message", "fact", "topic"]
    assert tokens_used == {"fact": 2, "topic": 2, "event": 0, "message": 2}


def test_pack_empty_hits():
    packed, tokens_used = pack_token_budget([], token_budget=100, memory_ratio=dict.fromkeys(MEMORY_TYPES, 0.25), k=5)
    assert packed == []
    assert tokens_used == {"fact": 0, "topic": 0, "event": 0, "message": 0}


def test_pack_carries_hit_tokens():
    """Every returned hit carries its token estimate unchanged."""
    hits = [_hit("message", "alpha beta gamma", 0.5, tokens=7)]
    packed, _ = pack_token_budget(hits, token_budget=100, memory_ratio={"message": 1.0}, k=5)
    assert packed[0].tokens == 7


@pytest.mark.parametrize("token_budget", [0, -1])
def test_pack_rejects_non_positive_budget(token_budget: int):
    with pytest.raises(ValueError):
        pack_token_budget([], token_budget=token_budget, memory_ratio={"message": 1.0}, k=5)
