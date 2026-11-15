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



## Issue 002 — Search don't have token limit

Search should return the x best result. Limited by the amount of token the client want (a parameter)
+ memory ratio


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