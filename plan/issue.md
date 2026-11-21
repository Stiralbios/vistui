# Known issues

## Issue 001 — Arbitrary code execution via chatgroup `ranking_formula` (critical)

- **Status:** OPEN — accepted for now (local, single-user deployment; do not expose chatgroup creation to untrusted users before fixing)
- **Reported / implemented-then-reverted:** 2026-11-15
- **Decision:** keep the simple `eval` implementation for now; revisit before any multi-user or public deployment
- **Where:** `sources/backend/search/scoring.py` (`evaluate_ranking_formula`), reachable through `GET /api/v1/chatgroups/{id}/search`
- **Entry point for the payload:** `ranking_formula` on `POST`/`PATCH /api/v1/chatgroups` — writable by any authenticated chatgroup owner

### Summary

Chatgroup ranking formulas are lambda expression strings stored per chatgroup and evaluated
by the hybrid search service. They are **user-authored input** (any authenticated user who
owns a chatgroup can set one), but they are run with `eval`:

```python
compiled = eval(formula, {"__builtins__": {}, **_SAFE_NAMESPACE})
```

The restricted namespace is **not** a sandbox. Python object attribute chains need no
builtins, so the classic escape works verbatim and reaches every loaded class (verified on
this codebase: 228 subclasses reachable):

```
lambda vector_score, bm25_score, salience, linked_count: ().__class__.__base__.__subclasses__()
```

From there it is a short hop to `subprocess` (e.g. picking the right subclass and calling
`os.system`/`Popen`) → arbitrary code execution on the backend, under the server's user.

### Impact

- Any authenticated user with a chatgroup can execute arbitrary Python on the server (read
  files, pivot to the Postgres, install packages, etc.).
- Also reachable indirectly: a *malicious formula stored in a shared/stolen DB* executes on
  the next search of that chatgroup — the payload persists in `chatgroup.ranking_formula`.

### Why it was accepted for now

The deployment is a local, single-user dev setup (local Ollama, dev Postgres, demo
accounts owned by one operator). The hardening (see below) was fully implemented and passing,
then reverted to keep `scoring.py` simple until the product needs the guarantee.

### Fix that was implemented, verified, then reverted

1. **Interpretation instead of execution** (the `ast.literal_eval` approach — that stdlib
   function is itself a recursive AST walker and never compiles):
   - `ast.parse(formula, mode="eval")`; require a single `ast.Lambda` declaring exactly the
     parameters `("vector_score", "bm25_score", "salience", "linked_count")` (no defaults,
     no varargs, no kwonly).
   - Whitelist-walk the lambda body accepting only: `Constant` (int/float only), `Name`
     (the 4 params + the helpers), `BinOp` (`+ - * / // % **`), `UnaryOp` (`+ -`),
     `BoolOp`, `Compare` (`== != < <= > >=`), `IfExp`, and `Call` (function must be a
     `Name` present in `_SAFE_NAMESPACE`; no keywords, no starred args).
     Everything else — `Attribute`, `Subscript`, comprehensions, nested lambdas, strings,
     f/t-strings, keyword calls — raises `ValueError`. The default `else` branch of the
     walker is the security boundary.
   - Interpret the validated AST node by node (numeric constants via `ast.literal_eval`),
     with **no `eval`/`compile` call in the module**: even a whitelist bug cannot escalate,
     because the interpreter exposes no node type that reaches attributes or unknown names.
   - Keep the `_SAFE_NAMESPACE` helpers so `DEFAULT_RANKING_FORMULA`
     (`max(vector_score, bm25_score) * salience * link_weight(linked_count)`) and existing
     stored formulas keep working unchanged.
2. **Reject on write**, not just at evaluation: pydantic `field_validator`s on
   `ChatGroupBase.ranking_formula` and `ChatGroupUpdate.ranking_formula` called the same
   validation, so `POST`/`PATCH /api/v1/chatgroups` answered `422` for malicious or
   malformed formulas before anything is stored.
3. **Tests** (to restore with the fix): sandbox escapes rejected
   (`().__class__...`, `open(...)`, `__import__`, subscript, comprehension, nested lambda,
   string constants), canonical-parameter enforcement, boolean/conditional semantics,
   422 on create with a hostile formula, 201 with a valid custom one.

The reverted implementation existed in the working tree on 2026-11-15 (uncommitted). If the
Phase 4 work has been committed since, the interpreter version is not in history — use the
design above to re-implement, or ask for the reverted diff.

### Reproduction (against a running backend)

```python
import requests
base = "http://localhost:8000/api/v1"
tok = requests.post(f"{base}/auth/jwt/login",
                    data={"username": "you@example.com", "password": "..."}).json()["access_token"]
requests.post(f"{base}/chatgroups", headers={"Authorization": f"Bearer {tok}"},
              json={"name": "pwn",
                    "ranking_formula": "lambda vector_score, bm25_score, salience, linked_count: "
                                       "().__class__.__base__.__subclasses__()"})
requests.get(f"{base}/chatgroups", ...)  # any search on the group evaluates the payload
```

=> idea:
In a settings.yaml we put all the formula we want and we just reference them in the db. Safer and less complex
than the llm proposed solution

## Issue 002 — Search don't have token limit

Search should return the x best result. Limited by the amount of token the client want (a parameter)
+ memory ratio computed by a decision model. % of token as fact, topic, message, events. Total 100%
Each result is added by type, until it reach it's token percent. Ordered by score. Only the text content count for the token amount.
Configurable message like for other prompts

## Issue 003 — Key embedding and salience in message processing state: do we really need them ?

Resolution (2026-11-15): no, they are redundant — same conclusion as for memory
objects. Events/topics/facts already show the model: their queue marker IS the NULL
column (`get_memory_objects_without_salience` selects `salience_score IS NULL`, and
the stage simply skips objects of opted-out chatgroups, which keep their NULL score
by design — no flag is needed to say "done"). Messages can work the same way:

- `embedding IS NULL` → waiting for the embedding stage; a failure leaves the column
  NULL, so retries come for free (`retry_count` / `last_failure_at` already carry the
  failure history).
- `embedding IS NOT NULL AND salience_score IS NULL` → waiting for the salience stage;
  opted-out chatgroups keep NULL forever and are skipped each round, exactly like
  memory objects.
- The consolidation gate becomes a column condition instead of
  `processing_state["salience"] = done`: a message is eligible when
  `embedding IS NOT NULL AND (salience_score IS NOT NULL OR the chatgroup opts out)`.
- `count_waiting_messages` derives the queue depth from the same composite conditions.

As built today the two keys still drive that gate, the queue count and the admin
status, so this is a recorded future simplification — not implemented for now
(user decision 2026-11-15). See `plan/plan4.md` §9 for the as-built state machine.

**Status (2026-11-21): DONE** — implemented as plan 5 Phase A
(`migrations/versions/0004_message_state_keys.py`, `sources/backend/batch/queries.py`,
see `plan/plan5.md` Results). Deviations from the sketch above, decided while
implementing:

- The salience opt-out for messages is enforced in the selection SQL
  (`_use_salience_optin`), not skipped per-round in Python: a permanent NULL must not
  re-enter the 5-per-round queue every round.
- `_use_salience_optin` is wrapped in `COALESCE(..., false)`: without the coalesce the
  expression is SQL NULL for opted-out rows (three-valued logic) and would silently
  drop them from both the queue and the consolidation gate.

**Corrected (2026-11-21, later the same day)** — the Phase A implementation wrongly
made embedding+salience a *consolidation gate* and keyed the salience queue on
"embedded with NULL salience". Corrected design (now in force):

- Consolidation eligibility is the protected window only; embedding and salience are
  enrichment stages with no say in it (the topic/fact candidate lookups simply offer
  no similar objects until the message is embedded).
- The salience queue marker is `salience_score IS NULL` alone.
- `use_salience` is a plain boolean (pydantic `bool`, JSON null rejected 422);
  migration `0006` normalizes legacy JSON nulls to false.


## Issue 004 — message send for salience should be configurable

To tweak how it answers.Configurable Like other prompts

**Status (2026-11-21): DONE** — implemented as plan 5 Phase C (see `plan/plan5.md`
Results). The salience decision question is fully chatgroup-configurable: the
`salience` prompts key holds the complete instruction template (`{kind}` substituted
with the element type, sent verbatim without the placeholder) and the new
`salience_rubric` prompts key holds the ordered rubric levels (2-10, 5-level default);
the score normalization (`score / (len(rubric) - 1)`) adapts to the rubric length.


## Issue 005 — no last_edited_at on message

If update is on edition such as salience or embeding or the state machine then last_edited_at is not changed. If the message content is actually replaced. Salience and embeding at None and last_edited at is updated
We also want to track when the message was actually sent or resent

Change last_edited_at to edited_at ?In all objects

**Status (2026-11-21): DONE** — implemented as plan 5 Phase B (migration `0005`, see
`plan/plan5.md` Results). Resolutions:

- No `sent_at` column (user decision 2026-11-21): `created_at` is the first-send time.
- `topic.last_edited_at`/`fact.last_edited_at` renamed to `edited_at`.

**Corrected (2026-11-21, later the same day)** — the Phase B implementation wrongly
limited `edited_at` to actual text changes and left it NULL until the first edit.
Corrected design (now in force, migration `0006`):

- `edited_at` records when `full_text` was last written: set at creation, bumped by
  every later write of the field (identical-text PATCHes included), on messages,
  events, topics and facts alike. Events gained the column.
- Requeue semantics are unchanged in spirit but keyed on content: only a text that
  actually differs from the stored one requeues embedding/salience/consolidation;
  role-only changes and identical text touch nothing derived.


## Issue 006 — no consolidation for fact / topic

todo in the far futur
Custom batch job to look for consolidation with search and similarity between topics/facts.
Right now the base just grow and grow. Hopefully the system is able to catch stuff that should be in the 
same topic/fact but all of this happen on message ingestion. There is nothing to consolidate if there is
too many similar ones


