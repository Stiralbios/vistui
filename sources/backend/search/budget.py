"""Token-budget packing for search hits and memory-ratio hygiene.

A search response is not just "the k best hits": the caller asks for a token budget,
split across the four memory types by a ratio (a per-search decision-model call or the
chatgroup's stored config). Packing walks the types in a fixed order (fact, topic,
event, message — densest first), takes each type's candidates in descending score
order while they fit the type's share, and the packed result is re-sorted globally by
score and capped by k.

One deviation from a strict budget: the single best hit of a type whose share is
positive is always included, even when it alone exceeds the share — a tiny budget
must not silence a whole memory type.

Token counts come from the local BPE tokenizer (tiktoken's `cl100k_base`, the same
library LangChain's text splitters use; the encoding data is fetched once and
cached). None of the served models actually uses this encoding — the count is a
deliberate approximate estimate, far tighter than a whitespace split, and clients
are expected to re-tokenize with their own model's tokenizer when exactness matters.
When the tokenizer cannot load (package missing, or the encoding data is unreachable
and not cached), estimation degrades to whitespace word counts.
"""

import tiktoken
from backend.search.schemas import SearchHit
from loguru import logger

# Packing order: densest memory first. The labels double as the criteria labels of
# the decision-model choice question (configured per chatgroup via the
# `memory_ratio_criteria` prompts key), so the answer's probabilities are keyed by
# them.
MEMORY_TYPES: tuple[str, ...] = ("fact", "topic", "event", "message")

# BPE encoding used for token estimates.
TOKENIZER_ENCODING = "cl100k_base"

_encoding: tiktoken.Encoding | None = None
_encoding_attempted = False


def _load_bpe_encoding() -> tiktoken.Encoding | None:
    """The BPE encoding for token estimates, or None when unavailable.

    Tried once per process: loading needs the encoding data on disk, downloaded on
    first use; a failure (offline first boot, missing package) must never break a
    search.
    """
    global _encoding, _encoding_attempted
    if not _encoding_attempted:
        _encoding_attempted = True
        try:
            _encoding = tiktoken.get_encoding(TOKENIZER_ENCODING)
        except Exception:
            logger.warning(f"token estimates fall back to whitespace words: cannot load {TOKENIZER_ENCODING}")
    return _encoding


def estimate_tokens(text: str) -> int:
    """BPE token count of a text, whitespace word count when the tokenizer is
    unavailable."""
    encoding = _load_bpe_encoding()
    if encoding is None:
        return len(text.split())
    return len(encoding.encode(text))


def normalize_probabilities(probabilities: dict[str, float]) -> dict[str, float] | None:
    """Turn decision-model probabilities into a memory ratio over the four types.

    Unknown labels are dropped, negative values clamped to zero, missing types count
    as zero, and the surviving values are renormalized to sum to 1. Returns None when
    nothing usable survives (empty input or a zero sum) — callers fall back to their
    stored config ratio.
    """
    values = {label: max(float(probabilities.get(label, 0.0)), 0.0) for label in MEMORY_TYPES}
    total = sum(values.values())
    if total <= 0:
        return None
    return {label: value / total for label, value in values.items()}


def pack_token_budget(
    hits: list[SearchHit],
    token_budget: int,
    memory_ratio: dict[str, float],
    k: int,
) -> tuple[list[SearchHit], dict[str, int]]:
    """Pack ranked hits into the per-type token shares of `token_budget`.

    `memory_ratio` maps memory types to fractions of the budget (a type absent from
    the map gets no share). The returned list is globally sorted by score and capped
    by k; `tokens_used` reports the whitespace token count of the returned hits per
    type. The single best hit of a type with a positive share is always included,
    even when it alone exceeds the share.
    """
    if token_budget < 1:
        raise ValueError(f"token_budget must be positive, got {token_budget}")

    packed: list[SearchHit] = []
    for label in MEMORY_TYPES:
        share = memory_ratio.get(label, 0.0) * token_budget
        if share <= 0:
            continue
        remaining = share
        for index, hit in enumerate(sorted((h for h in hits if h.type == label), key=lambda h: h.score, reverse=True)):
            if index > 0 and hit.tokens > remaining:
                break
            packed.append(hit)
            remaining -= hit.tokens

    packed.sort(key=lambda hit: hit.score, reverse=True)
    packed = packed[:k]
    tokens_used = {label: sum(hit.tokens for hit in packed if hit.type == label) for label in MEMORY_TYPES}
    return packed, tokens_used
