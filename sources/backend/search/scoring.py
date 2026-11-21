"""Score normalization and evaluation of chatgroup ranking formulas.

A chatgroup's `ranking_formula` names an entry of the operator's settings file (see
`backend.search.formulas`): a lambda expression that combines a candidate's vector
similarity, BM25 relevance, salience and linked count into a final score.

Both retrieval channels are normalized to 0..1 before a formula ever sees them: cosine
similarity is already in that range, while ParadeDB BM25 relevance is unbounded and grows
with term frequency and corpus statistics. Without normalization the BM25 channel (0..10+)
would drown the vector channel (0..1) in every formula.

Formulas are evaluated with plain `eval` in a namespace restricted to a few math helpers.
The restriction is for readability, not sandboxing (restricted namespaces are bypassable
without builtins); it is not a security boundary and does not need to be. Formulas are
operator-authored settings entries, and chatgroups reference them by name only, so no
user-authored string ever reaches this module.
"""

import math
from collections.abc import Callable
from typing import Any

FormulaCallable = Callable[[float, float, float, int], float]

# Compilation cache: formulas are admin-controlled strings evaluated once per distinct value.
_COMPILED: dict[str, FormulaCallable] = {}

# Link count at which `link_weight` reaches 1.0. Below it a candidate is discounted, above
# it the logarithm keeps growing slowly instead of multiplying the score linearly.
LINK_WEIGHT_REFERENCE = 100.0

# Helper functions available to formulas. Nothing else: no builtins are exposed.
_SAFE_NAMESPACE: dict[str, Any] = {
    "max": max,
    "min": min,
    "abs": abs,
    "log": math.log,
    "log10": math.log10,
    "sqrt": math.sqrt,
    "link_weight": None,  # replaced below, once link_weight is defined
}


def clamp_unit(value: float) -> float:
    """Clamp a score channel to the 0..1 range ranking formulas expect.

    NaN and infinities (e.g. cosine distance computed against a zero vector) become 0
    instead of poisoning every downstream max/sort with NaN.
    """
    if not math.isfinite(value):
        return 0.0
    return min(max(value, 0.0), 1.0)


def normalize_bm25(score: float, maximum: float) -> float:
    """Scale one BM25 relevance value to 0..1 against the best BM25 hit of the query.

    `score` is a raw ParadeDB score, `maximum` the best raw BM25 score retrieved for the
    same query across all memory types. A query without any BM25 match stays 0.
    """
    if score <= 0.0 or maximum <= 0.0:
        return 0.0
    return clamp_unit(score / maximum)


def link_weight(linked_count: int) -> float:
    """Logarithmic weight for how many memory objects a candidate is linked to.

    0 links -> 0.5 (an unconsolidated message stays findable instead of scoring 0),
    1 link -> 0.58, 10 links -> 0.76, 100 links -> 1.0, and the curve keeps growing
    slowly past the reference instead of multiplying the score linearly.
    """
    return 0.5 + 0.5 * math.log1p(max(linked_count, 0)) / math.log1p(LINK_WEIGHT_REFERENCE)


_SAFE_NAMESPACE["link_weight"] = link_weight


def evaluate_ranking_formula(
    formula: str, vector_score: float, bm25_score: float, salience: float, linked_count: int
) -> float:
    """Evaluate the given ranking formula for one candidate and return its final score.

    The channel arguments must already be normalized (see `clamp_unit` and
    `normalize_bm25`); `linked_count` is the raw link count, formulas decide how to weigh
    it, typically through `link_weight`. See the module docstring for the security
    caveat of evaluating user-authored formulas.
    """
    compiled = _COMPILED.get(formula)
    if compiled is None:
        if not formula.strip().startswith("lambda"):
            raise ValueError("ranking_formula must be a lambda expression")
        compiled = eval(formula, {"__builtins__": {}, **_SAFE_NAMESPACE})  # noqa: S307
        _COMPILED[formula] = compiled
    return float(compiled(vector_score, bm25_score, salience, linked_count))
