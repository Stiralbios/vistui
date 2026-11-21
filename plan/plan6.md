# Vistui Phase 6: Issue 001 — named ranking formulas in a settings file

Fixes issue 001 (arbitrary code execution via chatgroup `ranking_formula`, see
`plan/issue.md`) by moving formula authorship across the trust boundary: instead of
the reverted AST interpreter, chatgroup ranking formulas become **named entries in
the operator's settings file**, and the DB / API carry only names. The settings file
is the renamed `providers.yaml` → `settings.yaml`, gaining a `ranking_formulas`
section whose keys are usable in code a bit like the provider keys.

## Why this resolves 001

The vulnerability exists because a formula crosses a trust boundary: any
authenticated chatgroup owner writes a string that the server `eval`s. A settings
file is written by the machine operator — the same person who could edit
`scoring.py` or run arbitrary code on the host — so `eval` of settings-authored
formula text is trusted at the same level as the code itself and may stay. The fix
is closing the entry point, not the mechanism: the API accepts **names only**, a
hostile lambda source (the issue's `().__class__.__base__.__subclasses__` payload)
now 422s on write, and a stolen DB can at worst reference a name that is not defined
(it cannot smuggle code).

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Settings file | `git mv providers.yaml settings.yaml`; env var `VISTUI_PROVIDERS_FILE` → `VISTUI_SETTINGS_FILE`, default `./settings.yaml`; no back-compat | One operator-owned config file; the deployment is single-operator, a clean rename beats an alias |
| Model sections | yaml `defaults` → `defaults_models`, `overrides` → `overrides_models`; the `ProviderConfig` pydantic fields are renamed to match, and their consumers updated (`provider.py`, `test_provider.py`, `test_batch_integration.py`) | settings.yaml is a general settings file now — the `_models` suffix scopes those two sections to model selection, next to `providers` and `ranking_formulas` |
| Formula section | New top-level `ranking_formulas: {name: lambda text}` | Named entries referenced from code and API, like provider keys |
| Registry module | New `sources/backend/search/formulas.py`: loading, module cache, name→text resolution. `scoring.py` stays a pure text→score evaluator (only its docstring changes) | All formula knowledge lives in the search package; scoring stays free of config awareness |
| `eval` mechanism | Kept, for settings-authored text only; the restricted-namespace comment becomes "operator-trusted input" | Operator strings are as trusted as the code; the residual risk (never feed it non-settings strings) is documented in the issue instead |
| Built-in default | The default formula stays **in code**; the registry starts from the built-in `default` and yaml entries merge over it (yaml may override `default` or add names). No formula setup at all → built-in `default` alone | Zero-config operation keeps working; the code default can never be lost by trimming the yaml |
| Load-time validation | Every entry must `eval` to a lambda accepting exactly `(vector_score, bm25_score, salience, linked_count)`; a bad entry fails startup with a clear error | Operator typos surface at boot, not mid-search |
| DB column | `chatgroup.ranking_formula` keeps its name and `Text` type, stores the formula **name** (default `"default"`) | No column rename churn; new semantics documented in data-model |
| Migration | `0007`: rows equal to the old default lambda text → `"default"`; any other value raises with a clear message (operator registers the custom formula in settings.yaml first, re-runs). Downgrade maps `"default"` back to the old text, errors on other names | The migration cannot invent names; failing loudly beats silent breakage |
| Invalid formula name | A chatgroup set up with a name that is not in the registry **fails**: `ranking_formula_text(name)` raises `RankingFormulaInvalidProblem` — new `ProblemKind.INVALID` + `Entity.RANKING_FORMULA` in `exceptions.py`, mapped to **422** by the existing problem handler. The write path (POST/PATCH) 422s unknown names via the pydantic validator; GET still shows the stored name | Config drift fails loudly instead of silently scoring with another formula; write and search reject the same way |
| Code-facing names | `DEFAULT_RANKING_FORMULA_NAME = "default"` in `formulas.py`; the `DEFAULT_RANKING_FORMULA` text constant moves there from `chatgroup/schemas.py` | Seeders / factories / schemas reference the name; the text lives next to its registry |

## Target settings.yaml (excerpt)

```yaml
ranking_formulas:
  # The built-in default, spelled out; a chatgroup references it as "default".
  # The entry may be removed — the code carries the same default.
  default: >-
    lambda vector_score, bm25_score, salience, linked_count:
    max(vector_score, bm25_score) * salience * link_weight(linked_count)

providers:       # unchanged
defaults_models:  # was `defaults` — embedding/llm/decision model defaults
overrides_models: # was `overrides` — per-task model overrides
```

## Phase A — `settings.yaml` + the formula registry

### File plan

- `providers.yaml` → `settings.yaml` (git mv): add the `ranking_formulas.default` entry, update the file header comment.
- New `sources/backend/search/formulas.py`:
  - `RankingFormulaSettings(BaseSettings)`: `VISTUI_SETTINGS_FILE: str = "./settings.yaml"`
  - `DEFAULT_RANKING_FORMULA_NAME = "default"`; `DEFAULT_RANKING_FORMULA` (the lambda text, moved from `chatgroup/schemas.py`)
  - `load_ranking_formulas(path) -> dict[str, str]`: the built-in `default` merged with the yaml `ranking_formulas` entries (yaml may override `default` or add names; missing file/section → built-in default alone); every entry validated (lambda, exact 4 canonical parameters) with a clear `ValueError`
  - module cache + `get_ranking_formulas() -> dict[str, str]`, `reload_ranking_formulas(path=None)`, `ranking_formula_text(name) -> str` (unknown name → `RankingFormulaInvalidProblem`, no fallback)
- `sources/backend/exceptions.py`: new `ProblemKind.INVALID`, `Entity.RANKING_FORMULA`, `RankingFormulaInvalidProblem` (status 422); the existing problem handler maps it.
- `sources/backend/llm/config.py`: rename `VISTUI_PROVIDERS_FILE` → `VISTUI_SETTINGS_FILE` (default `./settings.yaml`), rename the yaml keys `defaults` → `defaults_models` and `overrides` → `overrides_models` together with the `ProviderConfig`/`from_env` field names, docstrings updated ("Loaded from settings.yaml"); behavior otherwise untouched. Consumers updated: `provider.py` (`config.defaults.*` → `config.defaults_models.*`, `config.overrides` → `config.overrides_models`), `test_provider.py`, `test_batch_integration.py` (`EMBEDDING_DIMENSIONS` accessor).
- `sources/backend/search/scoring.py`: module docstring rewrite — formulas are operator-authored settings entries referenced by name; `eval` stays because the API never accepts formula source; drop the "accepted for now" caveat.
- New `tests/backend/search/test_formulas.py`.
- `tests/backend/batch/test_batch_integration.py`: comment references to `providers.yaml` → `settings.yaml`.

### Steps (TDD)

1. RED — `tests/backend/search/test_formulas.py` (pure unit, no DB):
   - missing file (tmp_path + `VISTUI_SETTINGS_FILE`) → registry is `{"default": <built-in text>}`; `ranking_formula_text("default")` evaluates to the current scores
   - yaml with named formulas (folded scalar for a long one) → names resolve to their texts; `reload_ranking_formulas(path)` picks up a rewritten file
   - load validation: non-lambda entry, wrong parameter names, wrong arity → `ValueError` naming the entry
   - merge semantics: yaml overrides the built-in `default` when it defines its own `default`; a yaml without the key keeps the code default
   - unknown name: `ranking_formula_text("gone")` → `RankingFormulaInvalidProblem` (no fallback, no warning)
2. GREEN: `git mv`, write `formulas.py` + the exception, adjust `llm/config.py`, rewrite the `scoring.py` docstring.
3. Docs: `llm-provider.md` (rename throughout, env var row, `defaults_models`/`overrides_models` section renames, new `ranking_formulas` section + example, changelog), `infrastructure.md` (env var, docker example mounts settings.yaml, changelog if it has one).
4. `just fmt && just lint && just typecheck && just test`.

## Phase B — name-based ranking end-to-end

### File plan

- `sources/backend/chatgroup/schemas.py`:
  - `ChatGroupBase.ranking_formula: str = DEFAULT_RANKING_FORMULA_NAME` (import from `backend.search.formulas`; delete the local text constant)
  - `field_validator` on `ChatGroupBase` and `ChatGroupUpdate`: value must be a key of `get_ranking_formulas()` → 422 otherwise ("unknown ranking formula name"). `prevent_explicit_none` unchanged.
- `sources/backend/search/service.py`: score via `ranking_formula_text(chatgroup.ranking_formula)` instead of the raw column value.
- `sources/backend/seeders.py`, `tests/backend/chatgroup/factories.py`: use `DEFAULT_RANKING_FORMULA_NAME`.
- New `migrations/versions/0007_ranking_formula_names.py`: data-only migration per the Decisions table (old default text inlined in the migration, self-contained), up + down.
- Tests:
  - `tests/backend/search/test_search_apis.py`: pydantic default is the name; unknown name → 422 on POST and PATCH; known name accepted and echoed; the issue 001 payload lambda → 422 (the regression test); search ranks by a named formula (monkeypatched registry, e.g. `vector_only`); a chatgroup whose stored name is not in the registry → search answers **422** (`RankingFormulaInvalidProblem`, no fallback). `evaluate_ranking_formula` text-level tests (math helpers, conditionals, non-lambda rejection) keep feeding text directly — unchanged.
  - `tests/backend/batch/test_batch.py`: the custom formula becomes a registered name (monkeypatched registry) instead of inline lambda text.
- Run `just migrate` against the dev DB (inspect `chatgroup.ranking_formula` values first; expected all-default in dev).

### Steps (TDD)

1. RED — the schema / API / service tests above (422s, name defaults, named-formula ranking, invalid stored name → search 422).
2. GREEN: schemas validator + default, service resolution, seeders/factories, migration.
3. `just migrate` (dev DB), `just fmt && just lint && just typecheck && just test`, then `just test-integration` (uses the real settings.yaml via `get_provider_config`, now renamed).
4. Manual repro check from `plan/issue.md`: POST a chatgroup with the `subclasses` payload formula → 422; searching a chatgroup whose stored name is not defined → 422 problem response; the pre-existing chatgroup still scores.

## Phase C — docs + issue flip

- `api-contract.md`: create/patch request+response examples use `"ranking_formula": "default"`; validation rule becomes "must be a formula name defined in settings.yaml" (POST/PATCH 422) and search answers 422 when the stored name is not defined; changelog.
- `data-model.md`: `ranking_formula` column stores the formula name; creation default copied from system defaults stays true (the name); invariant #6 ("must be a Python lambda expression") becomes "must reference a formula defined in settings.yaml"; changelog.
- `retrieval-system.md`: § ranking — formulas are named entries in settings.yaml, evaluated with `eval` (operator-trusted); changelog.
- `documentations/architecture/backend/features.md`: one-line update if it mentions formula authorship.
- `plan/issue.md`: flip issue 001 to DONE with an as-built note (API users can only reference operator-defined names; `eval` retained by design for settings-authored text; residual: never feed non-settings strings to `evaluate_ranking_formula` — the write path enforces it).
- `plan/plan6.md`: Results section.

## Verification (every phase)

- `just fmt`, `just lint` (ruff), flake8 120-col (the prek hook), `just typecheck`, `just test` (hermetic), `just migrate` + `just test-integration` where DB-touching (dev 5433 / test 5434).

## Results

(to be filled during implementation)

## Results

### All phases (done 2026-11-22)

- **Phase A**: `settings.yaml` (git mv from `providers.yaml`) with `ranking_formulas`,
  `defaults_models`, `overrides_models`; new `backend/search/formulas.py` (registry:
  built-in `default` merged with settings entries, AST load validation — lambda over
  exactly the four canonical parameters, no defaults/varargs — env path
  `VISTUI_SETTINGS_FILE`, process cache, reload); `RankingFormulaInvalidProblem`
  (`ProblemKind.INVALID` + `Entity.RANKING_FORMULA`, 422) in `exceptions.py`; the
  registry loads at app startup so a broken settings file fails the boot;
  `llm/config.py` + `provider.py` + their tests renamed to the new file var and
  `defaults_models`/`overrides_models` fields; `scoring.py` docstring rewritten
  (operator-trusted input, no "accepted for now" caveat). 12 new registry unit tests.
- **Phase B**: `ChatGroupCreate` / `ChatGroupUpdate` gained the name validator (422
  unknown; `ChatGroupBase` deliberately left without one so `ChatGroupRead`/`Internal`
  never re-validate stored rows and GET keeps showing a dangling name);
  `search/service.py` scores through `ranking_formula_text(...)`; seeders, factories
  and the batch test helper store the name. Migration `0007` rewrites the built-in
  default source into `"default"` and aborts (up and down) on anything it cannot map,
  idempotently. 131 unit tests; the two stale dev-DB chatgroups (2026-11-15 manual
  test data carrying the old doc-example formula) were pre-normalized to the default
  text before migrating, per the migration's contract. Integration suite green
  (198s, real providers resolved through the renamed settings file).
- **Phase C**: api-contract (name-based create example, validation rule, search 422),
  data-model (column semantics + invariant 6), retrieval-system (Step 6 trust note),
  backend/features, llm-provider + infrastructure (Phase A), four changelogs; issue
  001 flipped DONE with the as-built note above.
- **Verification**: `just fmt` / `just lint` / `just typecheck` clean; `just test`
  131 passed + 1 deselected; `just migrate` applied `0007` on the dev DB;
  `just test-integration` green.
