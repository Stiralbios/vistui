"""Named ranking formulas: the operator's settings registry behind search scoring.

A ranking formula combines a candidate's vector similarity, BM25 relevance, salience
and linked count into a final score (evaluated by `backend.search.scoring`). Formulas
are operator-authored: named lambda expressions in the `ranking_formulas` section of
the settings file, referenced by name from chatgroups. The default formula also lives
in this module, so the registry always offers `default` — the settings section may
override it or add further names, and without any settings file the built-in default
serves alone.

Formulas are evaluated with plain `eval`. That is not a sandbox and does not need to
be: the settings file is written by the machine operator, trusted at the same level
as the code itself, and the API accepts formula names only, never formula source —
unknown names are rejected with 422 on write and raise at search time.
"""

import ast
from pathlib import Path

import yaml
from backend.exceptions import RankingFormulaInvalidProblem
from pydantic_settings import BaseSettings

DEFAULT_RANKING_FORMULA_NAME = "default"

# Both score channels are normalized to 0..1 by `backend.search.scoring` before the
# formula runs, and `link_weight` gives an unconsolidated item 0.5 and a 100-link
# item 1.0 (logarithmic in between).
DEFAULT_RANKING_FORMULA = (
    "lambda vector_score, bm25_score, salience, linked_count: "
    "max(vector_score, bm25_score) * salience * link_weight(linked_count)"
)

# The exact parameter list every formula must accept.
_FORMULA_PARAMETERS = ("vector_score", "bm25_score", "salience", "linked_count")


class RankingFormulaSettings(BaseSettings):
    """Path of the settings file holding the `ranking_formulas` section."""

    VISTUI_SETTINGS_FILE: str = "./settings.yaml"


def _validate_formula(name: str, text: str) -> None:
    """Check that a registry entry is a lambda accepting exactly the canonical
    parameters.

    Settings entries are operator-authored, so this is a fail-fast check, not a
    security one: a formula that cannot run, or that would be called with the wrong
    arguments, is reported at load time instead of failing mid-search.
    """
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"ranking formula {name!r} is not valid Python: {exc}") from exc
    if not isinstance(tree.body, ast.Lambda):
        raise ValueError(f"ranking formula {name!r} must be a lambda expression")
    args = tree.body.args
    params = [argument.arg for argument in (*args.posonlyargs, *args.args)]
    if (
        params != list(_FORMULA_PARAMETERS)
        or args.vararg is not None
        or args.kwarg is not None
        or args.kwonlyargs
        or args.defaults
        or args.kw_defaults
    ):
        raise ValueError(
            f"ranking formula {name!r} must be a lambda accepting exactly ({', '.join(_FORMULA_PARAMETERS)})"
        )


def load_ranking_formulas(path: str | Path | None = None) -> dict[str, str]:
    """Load the name-to-text registry: the built-in default merged with the
    `ranking_formulas` section of the settings file (settings entries win on name
    collisions).

    A missing file or section leaves the built-in default alone; an invalid entry
    raises ValueError naming it.
    """
    settings = RankingFormulaSettings()
    path = Path(path or settings.VISTUI_SETTINGS_FILE).expanduser()
    registry = {DEFAULT_RANKING_FORMULA_NAME: DEFAULT_RANKING_FORMULA}
    if path.exists():
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        entries = data.get("ranking_formulas") or {}
        if not isinstance(entries, dict):
            raise ValueError("ranking_formulas must be a mapping of names to lambda expressions")
        for name, text in entries.items():
            _validate_formula(name, text)
            registry[name] = text
    return registry


_formulas: dict[str, str] | None = None


def get_ranking_formulas() -> dict[str, str]:
    """The loaded formula registry, cached per process."""
    global _formulas
    if _formulas is None:
        _formulas = load_ranking_formulas()
    return _formulas


def reload_ranking_formulas(path: str | Path | None = None) -> dict[str, str]:
    """Force a reload of the formula registry from disk (or from `path`)."""
    global _formulas
    _formulas = load_ranking_formulas(path)
    return _formulas


def ranking_formula_text(name: str) -> str:
    """The lambda text registered under `name`.

    Unknown names raise RankingFormulaInvalidProblem: a chatgroup referencing an
    undefined formula fails its search instead of silently scoring with another
    formula.
    """
    registry = get_ranking_formulas()
    if name not in registry:
        raise RankingFormulaInvalidProblem(f"ranking formula {name!r} is not defined in the settings file")
    return registry[name]
