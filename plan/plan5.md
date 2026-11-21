# Vistui Phase 5: Fixes for issues 002, 003, 004, 005

**Goal:** Implement the four open issues from `plan/issue.md`:

- **003** — drop the redundant `embedding` / `salience` keys from `Message.processing_state`
  (NULL columns become the queue markers, as already resolved in the issue text).
- **005** — real edit tracking on messages (`edited_at` + `sent_at`), and rename
  `last_edited_at` → `edited_at` on topic/fact.
- **004** — make the full salience decision payload (instructions *and* rubric)
  configurable per chatgroup, like the other prompts.
- **002** — token-budget-limited search: per-type token shares from a memory ratio
  (config or decision model), packing ordered by score.

**Order and rationale:**

| Phase | Issue | Why this position |
|-------|-------|-------------------|
| A | 003 | Structural foundation: 005's "edit clears derived data" needs the column-based requeue (`embedding = NULL` re-enqueues, no key to reset). |
| B | 005 | Builds directly on A's requeue semantics; touches the same message model/store. |
| C | 004 | Small, isolated; touches `salience.py` after A has reshaped its selection/marking. |
| D | 002 | Largest new surface (search packing); independent of A–C except test fixtures. |

Each phase is committed separately: tests first, then code, migration, docs, and a flip
of the issue status in `plan/issue.md`.

---

## Phase A — Issue 003: NULL columns replace the `embedding` / `salience` state keys

### Decisions

| Topic | Decision |
|-------|----------|
| Queue markers | `embedding IS NULL` → waiting for the embedding stage. `embedding IS NOT NULL AND salience_score IS NULL` → waiting for the salience stage. A failed round rolls back, the column stays NULL, the next round retries for free (exactly the memory-object semantics of `get_memory_objects_without_salience`). |
| Remaining keys | `processing_state` keeps `event`, `topic`, `fact`, `retry_count`, `last_failure_at`. Consolidation has no column to flip, so those flags stay. `retry_count` / `last_failure_at` stop being updated for the embedding/salience stages — they never were per-message there (batch stages peek no message id); failures still show up in the admin `recent_failures` counter. |
| Opt-out in SQL, not Python | **Critical deviation from the issue sketch.** With no flag, opted-out messages would keep NULL salience forever, be re-selected every round and *starve* the 5-per-round salience queue (oldest-first selection never advances past them), and `count_waiting_messages` would never drain. The salience selection and the count queries therefore exclude opted-out chatgroups in SQL via a chatgroup join — same intent as "skipped each round", at zero cost. |
| Opt-in predicate | `_use_salience` semantics must survive translation to SQL: missing key → opted in (pydantic default true), JSON null or `false` → opted out. `COALESCE` cannot be used (JSON null is indistinguishable from missing through `->>`), so: `or_(~ChatGroupDO.config.haskey("use_salience"), ChatGroupDO.config["use_salience"].astext == "true")`. |
| Consolidation gate | `get_single_message_with_state` / `get_contiguous_waiting_messages` replace `processing_state['salience'] = 'done'` with `embedding IS NOT NULL AND (salience_score IS NOT NULL OR chatgroup opted out)`. Same eligibility as today, expressed as column conditions. |
| `POST /admin/reembed` | Simplifies to `UPDATE ... SET embedding = NULL` — the NULL *is* the requeue; drop the `jsonb_set` requeue of the `embedding` key. |
| Migration | `0004_message_state_keys`: `UPDATE message SET processing_state = processing_state - 'embedding' - 'salience'`. Downgrade re-derives both keys from the columns (`CASE WHEN embedding IS NULL THEN 'waiting' ELSE 'done' END`). No column changes. |
| Visible API change | `MessageRead.processing_state` no longer contains the two keys. No known consumer depends on them (frontend-agnostic API); documented in `api-contract.md`. |

### File plan

```
migrations/versions/0004_message_state_keys.py        # strip the two keys (downgrade re-derives)
sources/backend/message/schemas.py                    # default_processing_state: event/topic/fact only
sources/backend/batch/queries.py                       # new NULL-column queries + SQL opt-out predicate
sources/backend/batch/embeddings.py                   # set vector only, no key marking
sources/backend/batch/salience.py                     # set score only; no key marking; no Python opt-out skip
sources/backend/batch/worker.py                       # docstring: salience/embedding failures retry via NULL
sources/backend/admin/apis.py                          # reembed: NULL is the requeue
sources/backend/message/stores.py                     # update() reset dict loses the two keys (Phase B rewrites it fully)
sources/backend/seeders.py                            # _default_processing_state drops the two keys
tests/backend/message/factories.py                    # factory state dict
tests/backend/batch/test_batch.py                     # state fixtures, gate test, count test, worker test
tests/backend/batch/test_batch_integration.py         # processing_state assertions
tests/backend/search/test_search_apis.py              # seeded state dicts
tests/backend/admin/test_admin.py                      # reembed requeue assertion
```

### Steps (TDD)

1. Migration `0004` (upgrade strips keys, downgrade re-derives them from columns).
2. Tests first, in `test_batch.py`:
   - `get_messages_without_embedding` selects NULL-embedding messages oldest-first.
   - `get_messages_waiting_for_salience` selects embedded, NULL-salience messages of opted-in chatgroups only.
   - consolidation gate: embedded + salience NULL → not consolidated; salience set → consolidated; opted-out chatgroup with NULL salience → consolidated.
   - `count_waiting_messages`: embedding NULL counts; embedded + NULL salience counts for opted-in only; opted-out NULL-salience does not count; event/topic/fact waiting still count (protected window included).
   - failed decide call → salience_score stays NULL → message re-selected next round (rollback = retry).
3. Implement: `default_processing_state` drops the keys; new queries in `BatchQueries`:

   ```python
   # opted in: key missing (pydantic default true) or explicitly true — JSON null opts OUT,
   # so a plain COALESCE on ->>'use_salience' would be wrong.
   def _use_salience_optin() -> ColumnElement[bool]:
       return or_(
           ~ChatGroupDO.config.haskey("use_salience"),
           ChatGroupDO.config["use_salience"].astext == "true",
       )

   get_messages_without_embedding(limit)      # WHERE embedding IS NULL ORDER BY created_at, id
   get_messages_waiting_for_salience(limit)   # WHERE embedding IS NOT NULL AND salience_score IS NULL AND optin (chat→chatgroup join)
   ```

4. `embed_messages` / `SaliencePipeline.run_round` switch to the new queries and only set
   the column (no `processing_state` merge). The Python opt-out skip disappears from the
   message stage (SQL already filtered); `score_memory_objects` keeps its current
   behavior (out of scope — but see Notes).
5. Consolidation gate + `count_waiting_messages` rewritten as column conditions (above).
6. `admin/apis.py` reembed simplified; `seeders.py` state dict updated.
7. Update all fixtures/factories seeding `processing_state`; run `just test`; `just migrate`
   on the dev DB; run `just test-integration` (assertions on seeded state).
8. Docs: `batch-consolidation.md` state-machine section (3 flags + NULL-column semantics +
   SQL opt-out), `data-model.md` message `processing_state`, `api-contract.md` message shape.
   Flip issue 003 to RESOLVED in `plan/issue.md`.

---

## Phase B — Issue 005: message `edited_at`, rename `last_edited_at` → `edited_at`

### Decisions

| Topic | Decision |
|-------|----------|
| New message column | `edited_at` (nullable) — set **only when `full_text` actually changes** (value comparison against the stored text, not mere payload presence). Never-edited messages keep NULL. No `sent_at` column: "when actually sent or resent" tracking is dropped by user decision (2026-11-21) — `created_at` remains the first-send time, and `coalesce(edited_at, created_at)` derives "current content age" if a consumer ever needs it. |
| User edit (`PATCH /chats/{id}/messages/{id}`) | If the patched `full_text` differs from the stored value: `edited_at = now`, `embedding = NULL`, `salience_score = NULL` (requeues both stages for free thanks to Phase A), reset `event`/`topic`/`fact` to `waiting` and the retry metadata. A role-only change (or a PATCH carrying identical text) updates just that column: no `edited_at`, no requeue — role flips are administrative mislabel fixes, and a salience score computed with the old role is accepted staleness (noted; revisit if role edits ever matter to scoring). An empty PATCH changes nothing. |
| Internal updates | Embedding, salience and state-machine writes never touch `edited_at`. `updated_at` (onupdate) keeps tracking every row modification — `edited_at` is the user-facing "the content was really replaced" fact. |
| Rename | `topic.last_edited_at` → `edited_at`, `fact.last_edited_at` → `edited_at` (`ALTER TABLE ... RENAME COLUMN`). LLM rewrites of topics/facts *are* content edits, so the pipelines keep bumping it on create/update. |
| Event | No `edited_at`: events are never rewritten today (the extension feature from plan 4.2 was not implemented). Revisit with event continuity. |
| API break | `TopicRead`/`FactRead` field rename — fine pre-1.0, documented in `api-contract.md`. `MessageRead` gains `edited_at`. |

### File plan

```
migrations/versions/0005_message_timestamps.py         # add message.edited_at (nullable), rename topic/fact columns
sources/backend/message/models.py                       # edited_at column
sources/backend/message/schemas.py                      # MessageRead: edited_at
sources/backend/message/stores.py                       # update(): edit semantics (see above)
sources/backend/topic/models.py                         # edited_at rename
sources/backend/topic/schemas.py                         # TopicRead/TopicCreate/TopicUpdate: edited_at
sources/backend/fact/models.py                          # edited_at rename
sources/backend/fact/schemas.py                          # FactRead/FactCreate/FactUpdate: edited_at
sources/backend/batch/pipelines.py                      # last_edited_at= → edited_at= (topic/fact create/update)
tests/backend/message/test_message_apis.py              # edit detection + clearing, role-only no-op
tests/backend/message/factories.py, topic/factories.py, fact/factories.py
tests/backend/batch/test_batch.py                       # batch updates leave edited_at NULL
```

### Steps (TDD)

1. Tests first:
   - PATCH with a different `full_text` → `edited_at` set, `embedding`/`salience_score` NULL,
     event/topic/fact requeued.
   - PATCH role-only, and PATCH with identical text → column applied (role) / no-op, no
     `edited_at`, derived data untouched.
   - Batch embedding/salience/consolidation of a message → `edited_at` stays NULL.
   - Topic/fact reads expose `edited_at` (rename) and pipeline rewrites bump it.
2. Migration `0005` (upgrade: add nullable `edited_at`, rename topic/fact columns; downgrade:
   reverse). No backfill needed.
3. Model/schema/store changes per decisions. `MessageStore.update` becomes:

   ```python
   payload = message.model_dump(exclude_unset=True, exclude={"context", "id", "chat_id"})
   text_changed = "full_text" in payload and payload["full_text"] != orm_object.full_text
   for field, value in payload.items():
       setattr(orm_object, field, value)
   if text_changed:  # content actually replaced → record the edit and requeue everything
       orm_object.edited_at = dt.datetime.now(dt.timezone.utc)
       orm_object.embedding = None
       orm_object.salience_score = None
       orm_object.processing_state = default_processing_state()
   ```
4. `just test`, `just migrate`, `just test-integration`.
5. Docs: `data-model.md` (message/topic/fact columns), `api-contract.md` (message save/edit
   semantics, topic/fact field rename). Flip issue 005 to RESOLVED.

---

## Phase C — Issue 004: fully configurable salience decision payload

### Decisions

| Topic | Decision |
|-------|----------|
| What is configurable today | Only the instruction text (`chatgroup.prompts["salience"]`). The rubric (`SALIENCE_RUBRIC`, hardcoded 5 levels) — which *defines* "how it answers" — and the glued suffix `" Judge the {kind} in the state."` are not. |
| Instructions | `ChatGroupPrompts.salience` becomes the full instructions template, with a `{kind}` placeholder (`str.replace`, not `str.format` — a custom prompt with stray braces must not 500). New default: `"Judge how much the {kind} in the state is worth remembering for future conversations."` (the current default mentions a 0–1 score the decision model never produces). |
| Rubric | New `ChatGroupPrompts.salience_rubric: list[str]`, default = the current 5 levels moved to `chatgroup/schemas.py` as `DEFAULT_SALIENCE_RUBRIC`, validated to 2–10 levels (the `ScoreQuestion` bound). Both message and memory-object scoring use it. |
| Normalization | `salience = clamp_unit(answer.score / (len(rubric) - 1))` — adapts to custom rubric lengths. |
| Backward compat | Old chatgroups' `prompts` JSONB lacks the key → pydantic default fills it on read; `prompts.get("salience_rubric") or default` in the stage covers raw-DO access. |
| Routing | Unchanged: task stays `salience` → `local-ollama` / `d3:4b` via providers.yaml overrides. |

### File plan

```
sources/backend/chatgroup/schemas.py     # DEFAULT_SALIENCE_RUBRIC, salience default text, salience_rubric field + validator
sources/backend/batch/salience.py        # question built from chatgroup prompt+rubric; per-rubric normalization
tests/backend/batch/test_batch.py        # recorded-calls fake: custom prompt/rubric reach the decide call
tests/backend/chatgroup/test_chatgroup_apis.py  # rubric validation (1 / 11 levels → 422)
```

### Steps (TDD)

1. Tests first (extend the recorded-calls `_fake_decide(score, calls)` pattern):
   - custom rubric reaches `ScoreQuestion.criteria`; a 2-level rubric normalizes `score 1 → 1.0`.
   - custom prompt text (with and without `{kind}`) reaches `instructions` verbatim (minus the glued suffix).
   - default chatgroup → current 5 levels and default instructions.
   - `POST/PATCH /chatgroups` with a 1-level or 11-level rubric → 422.
2. Move `SALIENCE_RUBRIC` → `DEFAULT_SALIENCE_RUBRIC` in `chatgroup/schemas.py`; add the
   `salience_rubric` field (2–10) and the new default `salience` text to `ChatGroupPrompts`.
3. `salience.py`: `_question(chatgroup, kind)` builds instructions via
   `prompt.replace("{kind}", kind)` and criteria from the chatgroup's rubric; `_normalize`
   takes the rubric length; drop the hardcoded suffix. Applies to messages and memory objects.
4. `just test`; docs: `llm-provider.md` (salience task section: configurable rubric),
   `data-model.md` (chatgroup prompts). Flip issue 004 to RESOLVED.

---

## Phase D — Issue 002: token-budget-limited search with memory ratio

### Decisions

| Topic | Decision |
|-------|----------|
| Client parameter | `GET /chatgroups/{id}/search?q=&k=&token_budget=` — optional `token_budget` (int, ≥ 1); when absent it falls back to the chatgroup config's existing `token_budget` (2000). `k` stays as the total-hit cap (default 20). |
| Ratio source | **Decision model**, per the issue text: one `Provider.decide(task="memory_ratio")` call with state `{chatgroup: description, query: q}` and a single `ChoiceQuestion` whose criteria are the four types — the answer's native `probabilities` dict *is* the distribution. Runs only when `config.use_llm_memory_ratio` is true; otherwise the config's `memory_ratio` (message .40 / event .10 / topic .30 / fact .20) is used. This supersedes `retrieval-system.md` §2 (chat-model JSON ratio) — the doc is updated and the change noted in its changelog. |
| providers.yaml consequence | The `memory_ratio` override currently points at the gomodel **chat** model, which cannot proxy `/v1/systemone`. It must move to `local-ollama` / `d3:4b` (like `salience`). |
| Ratio hygiene | Probabilities renormalized to sum 1; missing type labels count as 0; `DecisionError`, absent/empty probabilities or a zero sum → fall back to the config ratio. The response reports which source was used (`ratio_source: "decision" | "config"`). |
| Packing | Per type (fact, topic, event, message): candidates in descending score order are added while their cumulative token estimate fits the type's share (`ratio × token_budget`); a share of 0 yields nothing for that type. The single best hit of a type whose share is > 0 is included even if it alone exceeds the share (otherwise a tiny budget could silence a whole memory type) — documented deviation from `retrieval-system.md` §7. The final `hits` list stays globally sorted by score and capped by `k`. |
| Token estimation | `estimate_tokens(text) = len(text.split())` — whitespace tokens of the `text` field only (per the issue: "only the text content counts"). Intentionally approximate; clients re-tokenize with their model's tokenizer. |
| Response | `SearchResponse` gains `token_budget`, `tokens_used` (per type + the packed hits carry a `tokens` field), `memory_ratio`, `ratio_source`. |
| Latency | `use_llm_memory_ratio` adds one d3 call per search (~5–10 s) — opt-in per chatgroup, `keep_alive="10m"` to keep the model warm. |

### File plan

```
sources/backend/search/budget.py            # NEW: estimate_tokens + pack_token_budget + ratio resolution helpers
sources/backend/search/schemas.py            # SearchResponse: token_budget/tokens_used/memory_ratio/ratio_source; SearchHit.tokens
sources/backend/search/service.py           # ratio resolution (decide call) + packing between ranking and truncation
sources/backend/search/apis.py              # token_budget query param
providers.yaml                               # memory_ratio override → local-ollama / d3:4b
tests/backend/search/test_budget.py         # NEW: packing unit tests
tests/backend/search/test_search_apis.py     # endpoint tests; _fake_decide answers choice questions
documentations/architecture/retrieval-system.md, api-contract.md, llm-provider.md
```

### Steps (TDD)

1. Tests first — `tests/backend/search/test_budget.py` (pure unit, no DB):
   - `estimate_tokens` counts whitespace tokens of the text only.
   - packing respects per-type shares, adds in score order, stops at the share, best-hit
     inclusion rule, 0-share type empty, `k` cap, empty ratio fallback normalization.
2. Implement `search/budget.py`; wire `service.search_chatgroup(chatgroup, q, k, token_budget)`:
   resolve ratio (config or decide) → existing candidate/rank flow → pack → sort by score → cap by `k`.
3. API param + response fields; extend `_fake_decide` in the search tests to answer the
   `memory_ratio` choice question with fixed probabilities (and record calls).
4. Endpoint tests:
   - tiny `token_budget` → hits truncated per share, `tokens_used` consistent, scores still
     globally sorted, `k` still respected.
   - `use_llm_memory_ratio=false` → no decide call for the ratio, `ratio_source="config"`.
   - `use_llm_memory_ratio=true` → decide called once with the choice question; probabilities
     drive the shares; fake decide raising → config fallback, `ratio_source="config"`.
5. `providers.yaml` override move; `just test`; `just test-integration` (one real search with
   the ratio decision against real d3, budget assertions).
6. Docs: `retrieval-system.md` (§2 decision-model ratio as built + §7 packing rules +
   changelog), `api-contract.md` (search params/response), `llm-provider.md` (memory_ratio
   decision task). Flip issue 002 to RESOLVED.

---

## Cross-cutting

- **Verification per phase:** `just fmt`, `just lint`, `just typecheck`, `just test`;
  `just up` + `just migrate` on the dev DB; `just test-integration` once after A+B and
  once after D (it exercises the real provider path end to end).
- **`plan/issue.md` bookkeeping:** flip each issue to RESOLVED with a date and a short
  as-built note in its section (the 003 entry already carries the resolution; update its
  status line).
- **Out of scope:** issue 001 (ranking formula eval — the settings.yaml registry idea is
  recorded in the issue but not scheduled here); issue 006 (topic/fact consolidation job);
  the same SQL-opt-out starvation fix for `score_memory_objects` (memory objects keep the
  Python skip today — worth a follow-up issue if opted-out memory objects pile up).

## Results

### Phase A — Issue 003 (done 2026-11-21)

`processing_state` no longer carries `embedding`/`salience` keys; the NULL columns are
the queue markers, like memory objects. Verified: `just fmt` / `lint` / `typecheck`,
92 unit tests + the real-provider integration test green, migration `0004` upgraded
and downgrade-validated on the dev DB (24/24 keys re-derived back, then stripped).

- `migrations/versions/0004_message_state_keys.py` — strips both keys
  (`processing_state - 'embedding' - 'salience'`); downgrade re-derives them from the
  columns via `jsonb_set`.
- `backend/batch/queries.py` — `BatchQueries` class: `get_messages_without_embedding`
  (`embedding IS NULL`), `get_messages_waiting_for_salience` (embedded + NULL score,
  opted-out chatgroups excluded in SQL), consolidation gate = embedded + salience ready
  (scored or opted out) + outside protected window, `count_waiting_messages` over the
  same column conditions.
- `backend/batch/embeddings.py` / `salience.py` — select via the new queries, set the
  columns, no state writes; failed rounds roll back (NULL = free retry).
- `backend/message/schemas.py` — `default_processing_state()` only keeps
  event/topic/fact + retry metadata.
- `backend/message/stores.py` — PATCH requeues via columns (nulls embedding and
  salience) + fresh consolidation flags.
- `backend/admin/apis.py` — `POST /admin/reembed` just nulls vectors now; seeder seeds
  NULL columns + consolidation-only state.
- Docs: `batch-consolidation.md`, `data-model.md`, `api-contract.md` updated to the
  column-queue model; issue 003 flipped to DONE in `plan/issue.md`.
- Gotcha fixed while testing: SQL three-valued logic made the opt-in predicate NULL
  for opted-out chatgroups — coalesced to a real boolean
  (see `plan/issue.md` issue 003 status).

### Phase B — Issue 005 (done 2026-11-21)

`message.edited_at` records real content edits; topics/facts use the same column
name. Verified: `just fmt` / `lint` / `typecheck`, 95 unit tests + the real-provider
integration test green, migration `0005` upgraded and downgrade-validated on the dev
DB (all 24 messages never-edited, so no backfill).

- `migrations/versions/0005_message_timestamps.py` — adds nullable `message.edited_at`,
  renames `topic`/`fact` `last_edited_at` → `edited_at` (both directions).
- `backend/message/{models,schemas}.py` — `edited_at` column + `MessageRead` field.
- `backend/message/stores.py` — update() computes `text_changed` by value comparison
  BEFORE applying the payload; only a real text change sets `edited_at`, nulls the
  embedding/salience columns and resets the consolidation flags. Role-only and
  identical-text PATCHes touch nothing derived.
- `backend/{topic,fact}/{models,schemas}.py` — field rename through models and
  `Read`/`Create`/`Update` schemas.
- `backend/batch/pipelines.py` — topic/fact create/update write `edited_at=now`
  (an LLM rewrite is a content edit).
- Tests: `test_message_apis.py` (text-change requeue, role-only no-op, identical-text
  no-op — derived data seeded directly in the DB), `test_batch.py` (worker round
  leaves `edited_at` NULL; topic rewrite bumps it and requeues its derived data),
  topic/fact API payloads + factories renamed.
- Docs: `data-model.md` (message/topic/fact columns, events never rewritten),
  `api-contract.md` (edit semantics + changelog), `batch-consolidation.md` (only
  text-changing PATCHes requeue). Also removed stale `search_vector` rows left from
  the BM25 migration. Issue 005 flipped to DONE in `plan/issue.md`.

### Phase C — Issue 004 (done 2026-11-21)

The salience decision payload is fully configurable per chatgroup. Verified: `just fmt` /
`lint` / `typecheck`, 98 unit tests + the real-provider integration test green (no
migration this phase — prompts JSONB only).

- `backend/chatgroup/schemas.py` — `DEFAULT_SALIENCE_RUBRIC` (the former hardcoded 5
  levels) + `ChatGroupPrompts.salience_rubric` (2-10 levels validated) + new default
  `salience` template: "Judge how much the {kind} in the state is worth remembering
  for future conversations." (the old default mentioned a 0-1 score the decision model
  never produces).
- `backend/batch/salience.py` — the rubric moved out; `_question(chatgroup, kind)`
  builds instructions via `str.replace("{kind}", kind)` (a template without the
  placeholder is sent verbatim, stray braces stay literal — no `str.format` 500s) and
  criteria from the chatgroup's rubric; `_normalize` takes the rubric length, so any
  rubric size maps to 0..1. Applies to messages and memory objects alike.
- Backward compat: old chatgroups' `prompts` JSONB lacks the keys → pydantic defaults
  on read, `prompts.get(...) or default` covers raw-DO access in the stage.
- Tests: default chatgroup → 5 levels + new default text; custom prompt/rubric reach
  the decide call verbatim and a 2-level rubric normalizes top score → 1.0;
  chatgroup API rejects 1-level and 11-level rubrics (422), stores custom prompt +
  rubric via PATCH. ChatGroup factory now derives config/prompts from the schemas
  instead of hardcoding a copy.
- Docs: `llm-provider.md` (template inputs + changelog), `data-model.md` (prompts
  table), `batch-consolidation.md` (rubric wording). Issue 004 flipped to DONE.

## Corrections (2026-11-21, post Phase C)

The user reviewed the as-built state of Phases A and B and corrected four design
points. Implemented as a correction pass across sources, tests, migration `0006`
(`event.edited_at` + backfill + NOT NULL + server default, legacy `use_salience` JSON
nulls normalized to false) and docs:

1. **Consolidation eligibility is the protected window only.** Embedding and salience
   are enrichment stages with no say in eligibility; the anchor/block queries drop the
   embedding+salience conditions. Consequence: the topic/fact candidate lookups can run
   on a message that is not embedded yet — they then offer no similar objects (cosine
   lookup returns [] for a NULL embedding), so the LLM can only create new ones.
   The search integration fixture changed accordingly (event spans all 10 seeded
   messages now: `linked_count == 10`).
2. **The salience queue marker is `salience_score IS NULL` alone** — the embedding
   stage is not a prerequisite for salience (`_salience_ready` deleted;
   `count_waiting_messages` drops the embedding condition).
3. **`use_salience` is a plain boolean** (missing key = pydantic default true; JSON null
   is rejected 422 by chatgroup POST/PATCH; legacy nulls normalized to false in
   migration `0006`).
4. **`edited_at` = when the text was last written.** Set at creation, bumped by every
   later `full_text` write (identical-text PATCHes included), on messages, events,
   topics and facts; events gained the column; topic/fact create/update payloads no
   longer accept it (server-managed). Requeue semantics unchanged: only a text that
   actually differs requeues embedding/salience/consolidation.

Verification: 97 unit tests (+1 message identity test, −1 obsolete) green, lint,
typecheck, real-provider integration test green (event block now = block size, 10/round),
migration `0006` down/up round-trip on the dev DB.

### Phase D — Issue 002 (done 2026-11-21)

Search packs its hits into a client-chosen token budget, split per memory type by a
configurable ratio. Verified: `just fmt` / `lint` / `typecheck`, 112 unit tests + the
real-provider integration test green (the integration test now also runs one real d3
memory-ratio decision and asserts the budget bookkeeping; 234s).

- `backend/search/budget.py` (new) — `MEMORY_TYPES` (packing order fact, topic, event,
  message), `RATIO_CRITERIA` (choice-question criteria labels + descriptions),
  `estimate_tokens` (whitespace split), `normalize_probabilities` (drop unknown labels,
  clamp negatives, missing labels 0, renormalize to 1; None on empty/zero sum) and
  `pack_token_budget` (per-type greedy in score order, stop at first misfit, best-hit
  inclusion for positive shares, global re-sort + `k` cap; `tokens_used` over the
  returned hits).
- `backend/search/service.py` — `_memory_ratio()` resolves the ratio: config verbatim,
  or one `decide(task="memory_ratio")` choice question (state: chatgroup description +
  query; criteria: the four types) when `use_llm_memory_ratio` is true, with DecisionError
  / absent-probability / zero-sum fallback to config; `search_chatgroup` returns the
  full `SearchResponse` now.
- `backend/search/schemas.py` — `SearchHit.tokens`; `SearchResponse` gains
  `token_budget`, `tokens_used`, `memory_ratio`, `ratio_source`
  (Literal["config", "decision"]).
- `backend/search/apis.py` — `token_budget: int | None = Query(ge=1)`, falling back to
  `chatgroup.config.token_budget`.
- `providers.yaml` — `memory_ratio` override moved from the gomodel chat model to
  `local-ollama` / `d3:4b` (gomodel cannot forward `/v1/systemone`).
- Tests — `test_budget.py` (11 pure unit tests: estimation, renormalization,
  per-type packing, best-hit rule, zero shares, global sort + k, bookkeeping);
  `test_search_apis.py`: the fake decide answers the choice question with recorded
  calls (fixture refactored into a reusable `_fake_provider` contextmanager) + 4 new
  endpoint tests (packing with budget 8 exercising the best-hit rule, validation 422,
  decision ratio with 4:1 probabilities renormalizing to 0.8/0.2 and dropping the
  event/fact hits, config fallback on decision error and on absent probabilities);
  the integration test flips the demo chatgroup to `use_llm_memory_ratio` and asserts
  the real distribution (four types, sums to 1) and the packing invariants.
- Docs — `retrieval-system.md` (§2 decision-model ratio as built, §7 packing rules +
  best-hit deviation, §8 response, perf target, BM25 `full_text` correction +
  changelog), `api-contract.md` (search endpoint + response example + changelog),
  `llm-provider.md` (decision-task override, template-inputs row, changelog),
  `data-model.md` (config/prompts rows). Issue 002 flipped to DONE.

### Phase D revision (2026-11-21, same day)

Two refinements after the Phase D review, both verified (fmt / lint / typecheck, 115
unit tests, real-provider integration green):

- **BPE token estimates**: `estimate_tokens` counts tiktoken `cl100k_base` pieces
  (the local tokenizer LangChain's text splitters use; already in the tree via
  langchain-openai, now a direct dependency) instead of whitespace words. The
  encoding data is fetched once and cached; when it cannot load (offline first boot),
  estimation degrades to whitespace word counts rather than failing the search. The
  packing tests now pass explicit token counts, keeping them estimator-agnostic; the
  estimation tests pin cl100k_base exacts plus the fallback.
- **Configurable ratio criteria**: the choice question's criteria moved from the
  `RATIO_CRITERIA` constant in `budget.py` to the chatgroup prompts, mirroring
  `salience_rubric`: `ChatGroupPrompts.memory_ratio_criteria` (default
  `DEFAULT_MEMORY_RATIO_CRITERIA` in `chatgroup/schemas.py`), keys validated to be
  memory types with at least two entries (422 otherwise). Labels stay fixed to the
  four types — the model's probability keys and the packing shares are looked up by
  them — while chatgroups reword the descriptions; a type left out of the criteria
  gets no share. No migration (prompts JSONB, pydantic defaults fill on read).
