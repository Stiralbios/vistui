"""Unit tests for the named ranking formula registry (no DB, no network)."""

from pathlib import Path

import pytest
import yaml
from backend.exceptions import RankingFormulaInvalidProblem
from backend.search.formulas import (
    DEFAULT_RANKING_FORMULA,
    DEFAULT_RANKING_FORMULA_NAME,
    get_ranking_formulas,
    ranking_formula_text,
    reload_ranking_formulas,
)
from backend.search.scoring import evaluate_ranking_formula

_VECTOR_ONLY = "lambda vector_score, bm25_score, salience, linked_count: vector_score"


def _write_settings(path: Path, entries: object) -> Path:
    path.write_text(yaml.safe_dump({"ranking_formulas": entries}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _built_in_registry(tmp_path: Path):
    """Start every test from the built-in registry (no settings file), and leave it
    behind so other suites never inherit a tmp_path registry."""
    reload_ranking_formulas(path=tmp_path / "missing.yaml")
    yield
    reload_ranking_formulas(path=tmp_path / "missing.yaml")


def test_missing_file_leaves_built_in_default(tmp_path: Path):
    """Without any settings file the registry still offers the built-in default,
    and it evaluates like the formula the code has always carried."""
    assert get_ranking_formulas() == {DEFAULT_RANKING_FORMULA_NAME: DEFAULT_RANKING_FORMULA}
    assert ranking_formula_text("default") == DEFAULT_RANKING_FORMULA
    assert evaluate_ranking_formula(ranking_formula_text("default"), 0.8, 0.4, 1.0, 0) == pytest.approx(0.4)


def test_yaml_entries_merge_over_built_in(tmp_path: Path):
    """Settings entries add names and may override the built-in default; a section
    without a `default` entry keeps the code's."""
    _write_settings(
        tmp_path / "settings.yaml",
        {"vector_only": _VECTOR_ONLY, "default": "lambda vector_score, bm25_score, salience, linked_count: 1.0"},
    )
    reload_ranking_formulas(path=tmp_path / "settings.yaml")
    registry = get_ranking_formulas()
    assert set(registry) == {"default", "vector_only"}
    assert ranking_formula_text("vector_only") == _VECTOR_ONLY
    assert ranking_formula_text("default") == "lambda vector_score, bm25_score, salience, linked_count: 1.0"

    _write_settings(tmp_path / "settings.yaml", {"vector_only": _VECTOR_ONLY})
    reload_ranking_formulas(path=tmp_path / "settings.yaml")
    assert ranking_formula_text("default") == DEFAULT_RANKING_FORMULA


def test_settings_file_resolved_from_env(monkeypatch, tmp_path: Path):
    """Without an explicit path, the registry loads the file named by
    VISTUI_SETTINGS_FILE."""
    _write_settings(tmp_path / "settings.yaml", {"vector_only": _VECTOR_ONLY})
    monkeypatch.setenv("VISTUI_SETTINGS_FILE", str(tmp_path / "settings.yaml"))
    reload_ranking_formulas()
    assert ranking_formula_text("vector_only") == _VECTOR_ONLY


def test_reload_picks_up_file_changes(tmp_path: Path):
    """A reload re-reads the file: removed names disappear."""
    settings = _write_settings(tmp_path / "settings.yaml", {"first": _VECTOR_ONLY})
    reload_ranking_formulas(path=settings)
    assert "first" in get_ranking_formulas()
    _write_settings(settings, {"second": _VECTOR_ONLY})
    reload_ranking_formulas(path=settings)
    assert "first" not in get_ranking_formulas()
    assert "second" in get_ranking_formulas()


@pytest.mark.parametrize(
    "entry",
    [
        "max(vector_score, bm25_score)",  # not a lambda
        "lambda a, b, c, d: 1.0",  # wrong parameter names
        "lambda vector_score: 1.0",  # wrong arity
        "lambda vector_score, bm25_score, salience, linked_count=1: 1.0",  # defaults
        "lambda vector_score, bm25_score, salience, *linked_count: 1.0",  # varargs
        "lambda vector_score, bm25_score, salience, linked_count: (",  # syntax error
    ],
)
def test_load_rejects_invalid_entries(tmp_path: Path, entry: str):
    """Every entry must be a lambda accepting exactly the four canonical parameters;
    anything else fails the load naming the entry."""
    settings = _write_settings(tmp_path / "settings.yaml", {"broken": entry})
    with pytest.raises(ValueError, match="broken"):
        reload_ranking_formulas(path=settings)


def test_load_rejects_non_mapping_section(tmp_path: Path):
    """The ranking_formulas section must be a mapping of names to lambdas."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({"ranking_formulas": ["a", "b"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        reload_ranking_formulas(path=settings)


def test_unknown_name_raises_problem():
    """Resolving a name that is not registered fails instead of falling back to
    another formula."""
    with pytest.raises(RankingFormulaInvalidProblem) as excinfo:
        ranking_formula_text("gone")
    assert "gone" in excinfo.value.detail
