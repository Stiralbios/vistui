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


## Issue 003 — Key embedding and sallience in message processing state: do we really need them ?

State after plan 4.4 (2026-11-15): both flags drive the state machine, not the columns.
`embedding`/`salience` = done is the gate for event/topic/fact consolidation (the worker
marks them done in the same round before consolidating), and `count_waiting_messages`
uses them for the queue depth, so removing the keys would stall consolidation and the
admin status. The columns themselves are independent: `salience_score` stays NULL for
not-yet-scored rows and for chatgroups with `use_salience` false/null (neutral 1.0 at
scoring time), and `embedding` NULL just excludes a row from the vector channel. See
`plan/plan4.md` §9.