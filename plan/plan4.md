# Vistui Phase 4: Model Upgrades, Event Continuity, BM25 Search, Salience

**Goal:** Switch the embedding model to `embeddinggemma-2:740m`, make the last Event of a chat a living summary that grows with the conversation (never touching the protected window), replace the dead `search_vector` column with ParadeDB BM25 indexes over raw text, add real salience scoring with the `d3:4b` decision model, and split consolidation into a cheap decision step (`d3:4b`) followed by a generation step (`gemma4:e4b`).

**Scope focus:** retrieval quality is not the goal of this phase — but a minimal hybrid search endpoint ships as the validation vehicle for everything else (embedding A/B, salience scores, BM25 indexes, ranking formula).

---

## 1. Decisions

| Topic | Decision |
|-------|----------|
| Embedding model | `embeddinggemma-2:740m` (natively 768-dim — matches `Vector(768)` schema; no truncation parameter needed). Measured warm on the current CPU-only Ollama host: ~0.10s vs ~1.69s per query for `qwen3-embedding:8b` (~17× faster). Switch is config-only (`providers.yaml`); revert by pointing back at `qwen3-embedding:8b`. |
| Existing vectors | Embeddings from different models are not comparable: switching requires re-embedding everything. New admin endpoint `POST /admin/reembed` nulls all vectors and requeues them; the worker refills over the next rounds. |
| Event continuity | The most recent Event of a chat is **edited/extended** when the next eligible block continues it, instead of always creating a new Event. Decision (extend vs new block) by `d3:4b`; rewritten summary by `gemma4:e4b`. |
| Protected window | Unchanged invariant: the `protected_message_count` (default 10) most recent messages of a chat never enter an Event until they leave the window (already enforced by `_is_outside_protected_window()` in `queries.py`). Extension only ever touches eligible messages. |
| BM25 | ParadeDB `bm25` index over the raw `full_text` column (tokenization is handled by the index; rows are indexed automatically on insert/update). The `search_vector` TSVECTOR column is **dropped**: nothing ever wrote it, and hybrid fusion already happens at the ranking-formula level (`vector_score`, `bm25_score`), so a separate Postgres-native tsvector is redundant. Re-add later only if Postgres-native `ts_rank` scoring is ever needed. |
| Salience | Real scoring via `d3:4b`: new batch stage scores messages (and memory objects) 0.0–1.0. The Phase 3 short-circuit is removed: the seeder no longer pre-marks salience `done` and no longer hardcodes `0.5`. `ChatGroupConfig.use_salience` false/null opts out: the stage only marks `done`, `salience_score` stays NULL, and the search formula passes the neutral salience (1.0) for those rows. |
| Decision model | `d3:4b` handles all create/update/skip decisions (topic candidates, fact candidates, event grouping boundaries, extend-vs-new). `gemma4:e4b` writes all summaries. This maps onto the existing prompt/task split (`topic_candidate` vs `topic_update`, `fact_candidate` vs `fact_update`). |
| Failure handling | Decision calls that return nothing parseable degrade to a safe default (extend → new; candidate → skip) and the message still completes. Generation failures keep the existing worker semantics (mark failed, count, retry count). |
| Per-task models | `providers.yaml` overrides updated: `salience`, `topic_candidate`, `fact_candidate`, `event_decision` (new task) → `d3:4b`; `event`, `topic_update`, `fact_update` → `gemma4:e4b`. |

---

## 2. Dependencies

No new Python dependencies — `sqlalchemy-paradedb>=0.13.0` already ships everything needed:

- `paradedb.ParadeDBField` / `paradedb.IndexOptions` — BM25 index DDL (models + alembic)
- `paradedb.all/term/phrase/parse` — BM25 predicates; `paradedb.score` — BM25 relevance score
- `paradedb.cosine_distance` — already used for vector search

Ollama models required on `http://192.168.9.137:11434/v1` (both already pulled, verified via `/v1/models`):

- `embeddinggemma-2:740m` — embeddings (768-dim)
- `d3:4b` — decision model (salience + create/update/skip decisions)

---

## 3. File plan

### New files

```
sources/backend/batch/salience.py     # SaliencePipeline (messages) + score_memory_objects
sources/backend/search/
  __init__.py
  apis.py                            # GET /chatgroups/{id}/search — hybrid vector+BM25
  schemas.py                         # SearchQuery, SearchHit models
  scoring.py                         # ranking_formula evaluation + score normalization

migrations/versions/0003_bm25_indexes.py   # create BM25 indexes, drop search_vector
```

### Modified files

```
providers.yaml                       # embedding model + per-task decision model overrides
sources/backend/llm/prompts.py       # SALIENCE, EVENT_DECISION, EVENT_SUMMARY,
                                      # TOPIC_CANDIDATE (decisions only), TOPIC_UPDATE, etc.
sources/backend/chatgroup/schemas.py # updated default prompt texts
sources/backend/batch/pipelines.py   # event extension, decide/generate split, no 0.5 hardcode
sources/backend/batch/queries.py     # get_last_event(), salience-aware count_waiting_messages
sources/backend/batch/worker.py      # new stages: salience, memory_salience
sources/backend/batch/__init__.py    # export SaliencePipeline
sources/backend/seeders.py           # salience back to waiting (real pipeline exists)
sources/backend/message/models.py    # drop search_vector, add BM25 index to __table_args__
sources/backend/event/models.py      # same
sources/backend/topic/models.py      # same
sources/backend/fact/models.py       # same
sources/backend/admin/apis.py        # POST /admin/reembed
sources/backend/main.py              # register search router
tests/backend/batch/test_batch.py             # new fakes + tests for salience/extension/decisions
tests/backend/batch/test_batch_integration.py # updated assertions (salience, extension)
```

---

## 4. Implementation steps

### 4.1 Embedding model switch + re-embed helper

- `providers.yaml`: `defaults.embedding.model: "embeddinggemma-2:740m"` (dimensions stay 768).
- `Provider.embed()` already validates dimensions — no change.
- New endpoint `POST /admin/reembed` (admin router, alongside seed/trigger):

```python
# 1. Messages: null the vector, requeue the embedding stage
UPDATE message SET embedding = NULL,
                    processing_state = jsonb_set(processing_state, '{embedding}', '"waiting"');
# 2. Memory objects: null the vector (embed_memory_objects picks up NULLs)
UPDATE event SET embedding = NULL;  -- same for topic, fact
```

- The worker refills everything within one round per 32 rows (demo data: 1 round).
- Timing note: with the 740M model a full 30-message batch embeds in ~1–2s (was ~40s), which also removes most of the Ollama contention we hit when running the app and integration tests concurrently.

### 4.2 Event continuity (extend the last event)

- New query in `BatchQueries.get_last_event(session, chat_id) -> EventDO | None` — newest by `end_at`, tie-break `id`.
- `EventPipeline.run_round` becomes a two-step decide/generate flow:

1. Build the eligible block exactly as today (anchor + contiguous waiting, embedded, outside the protected window, ≤ `BLOCK_SIZE`). Protected messages are structurally excluded — this is the "never go into an event" guarantee and gets its own regression test.
2. Load the chat's last event (if any).
3. **Decide (`d3:4b`, task `event_decision`)**: given the last event's summary/`end_at` and the new block, return either
   `{"action": "extend"}` — the block continues the same conversational event — or
   `{"action": "new", "groups": [{"message_ids": [...]}, ...]}` — boundaries inside the block.
   No last event, or a `d3` failure → default to `{"action": "new"}` with a single whole-block group (today's fallback).
4. **Generate (`gemma4:e4b`, task `event`)**:
   - extend → new prompt `EVENT_SUMMARY` gets the old summary + new messages and returns the rewritten summary. Update the existing `EventDO`: `full_text`, `end_at = max(end_at, block end)`, link the new messages, **clear `embedding`** (same staleness semantics as topic/fact updates; the next round re-embeds). No new row.
   - new → one `EVENT_SUMMARY` call per group (or a single call for the whole block as today), create events as today.
5. Mark the block `event = done` as today.

Invariants:

- The last event is only ever edited when *eligible* messages continue it; the protected window stays untouchable, so the "current" conversation tail never gets summarized until it ages out.
- Extension never touches messages that are already `event = done` (except by adding new links).
- Editing the event bumps `updated_at` via `onupdate`; events have no `last_edited_at` column (topics/facts semantics stay as they are).

### 4.3 BM25 indexes + drop `search_vector`

Migration `0003_bm25_indexes`:

- Drop `search_vector` from `message`, `event`, `topic`, `fact`; remove the columns from the 4 models (test DB follows via `create_all`).
- Create one BM25 index per table over raw text, with key/filter fields:

```python
from paradedb import ParadeDBField

Index(
    "message_bm25_idx",
    ParadeDBField(message.c.id),
    ParadeDBField(message.c.full_text),
    ParadeDBField(message.c.chat_id),      # keyed field: filter by chat
    postgresql_using="paradedb",
)
# event/topic/fact: id + full_text + chat_id (event) / chatgroup_id (topic, fact)
```

- ParadeDB maintains the index automatically on insert/update — no triggers, no columns to keep in sync.
- Why dropping is safe: no code has ever read or written `search_vector`; hybrid ranking takes `bm25_score` straight from `paradedb.score(full_text)` and `vector_score` from cosine distance; a Postgres-native tsvector path can be reintroduced later if `ts_rank`-based fusion is ever wanted.

### 4.4 Salience with the decision model — DONE (2026-11-15)

As built (the original sketch — a chat completion returning `{"salience": <float>}` —
was superseded by the System One discovery, see §8):

- `providers.yaml` override: `salience: {provider: local-ollama, model: "d3:4b"}` — a
  *decision task* resolved by `Provider.decide(task="salience")` against the same
  overrides map as chat tasks, falling back to `defaults.decision`.
- `backend/batch/salience.py`: `SaliencePipeline.run_round(limit=10)` takes the oldest
  waiting messages and scores them **element per element** (user decision, like the
  other batch stages): one `decide()` call per message with state
  `{chatgroup, message: {role, text}}`, a single `ScoreQuestion` named `salience`
  built from the chatgroup's `prompts["salience"]` plus "Judge the message in the
  state.", against a fixed 5-level rubric (trivial → critical). The probability-weighted
  rubric index normalized by `len - 1` gives the 0..1 salience. `keep_alive="10m"`
  keeps d3 warm across the calls of a round, and `local-ollama.timeout_seconds` went
  120 → 300 (only decision calls use it). The first cut batched the group's messages
  into chunked calls of 5 questions against a shared state; element-per-element is
  simpler and faster (every question is scored against the whole state, so batched
  calls grow superlinearly).
- New stage `score_memory_objects(limit=32)`: events/topics/facts with
  `salience_score IS NULL` scored from `full_text` the same way — one `decide()` call
  per object (state key `event`/`topic`/`fact`). The pipelines
  stopped hardcoding `salience_score=0.5` (creates leave NULL) and topic/fact updates
  clear it alongside the embedding for regeneration.
- `use_salience` false/null opts out: messages only get the flag marked `done`,
  `salience_score` stays NULL, no decision call — search passes neutral 1.0.
- Seeder: `_default_processing_state()` sets `salience: waiting`, no placeholder score.
- `count_waiting_messages()` includes the `salience` flag; the documented
  embedding+salience-done gate for event/topic/fact consolidation is now enforced in
  `queries.py` (salience runs earlier in the same round, so a message is scored before
  it is consolidated).
- Live runs against real d3:4b on the dev DB — batched first cut: 10 messages in 2
  chunked calls (246s) + 10 memory objects in 3 calls (160s); element-per-element:
  4 messages in 4 calls (37s, ~9s each). Scores are sensible: hiking plans/preferences
  ~0.3–0.66, minor context ~0.2–0.4, stale "Demo event" placeholders 0.06–0.22.
- Worker round becomes:

```
1. embed_messages            (embedding)
2. embed_memory_objects      (memory_embedding)
3. SaliencePipeline.run_round (salience)
4. score_memory_objects       (memory_salience)
5. EventPipeline.run_round    (event)
6. TopicPipeline.run_round    (topic)
7. FactPipeline.run_round     (fact)
```

### 4.5 Decision model for create/update choices (topics & facts)

And to choose to extend or create a new event. And memory ratio. And basically anything that output something else than fee text
Restructure `TopicPipeline`/`FactPipeline` into decide → generate, mapping onto the existing prompt pair:

| Step | Model (task) | Output |
|------|--------------|--------|
| Candidate decision | `d3:4b` (`topic_candidate` / `fact_candidate`) | `[{"action": "create"|"update"|"skip", "topic_id": ...}]` — decisions only, no summaries |
| Update/create generation | `gemma4:e4b` (`topic_update` / `fact_update`) | `{"summary": "..."}` — the new text, given the message + current summary |

- `_extract_actions()` normalization stays as the tolerant parser for `d3`'s output shape (it already accepts bare objects, wrapped keys, bare arrays).
- Empty/unparseable decision response → treat as skip, message still completes (`topic`/`fact` = done) — never fail a message on a decision no-op.
- For creates: insert with `embedding = NULL` and `salience_score = NULL`; the batch stages fill both next round.
- For updates: rewrite `full_text`, bump `last_edited_at`, clear `embedding` — unchanged semantics, now with the generation model writing the text.
- `TOPIC_CANDIDATE`/`FACT_CANDIDATE` default prompts are rewritten to ask for decisions only; `TOPIC_UPDATE`/`FACT_UPDATE` defaults become real rewrite prompts (the current `ChatGroupPrompts` texts already describe exactly this split).

### 4.6 Hybrid search endpoint (validation vehicle)

`GET /chatgroups/{chatgroup_id}/search?q=...&k=20`

1. Embed the query with the provider (768-dim).
2. Per memory type (message, event, topic, fact), two candidate sets of at most 20 rows each:
   - BM25 candidates: `WHERE parse(Model.full_text, q)` scoped by `chat_id`/`chatgroup_id`, with `paradedb.score(full_text)` as the raw BM25 score. (The draft's `all() & parse()` is a no-op conjunction, so the implementation uses bare `parse()`; the user's query tokens are phrase-quoted first because ParadeDB's query-string parser is Lucene syntax — a bare `a"b`, `field:value`, `boots*` or `NOT` raises a parser error, which surfaced as a 500.)
   - Vector candidates: `ORDER BY cosine_distance(embedding, query_vec)` → `vector_score = 1 - distance`.
3. **Normalize both channels to 0..1** before ranking: cosine similarity is already in range, ParadeDB BM25 is unbounded, so the query's best BM25 hit across all types is scaled to 1.0 (`search.scoring.normalize_bm25`, `clamp_unit`). Without this, BM25 magnitudes (~0–10+) drown the vector channel in every formula.
4. Fuse per row with the chatgroup's `ranking_formula`: `eval` the stored lambda with `(vector_score, bm25_score, salience, linked_count)` in a namespace restricted to the math helpers of `search.scoring._SAFE_NAMESPACE` (no builtins). `use_salience = false` groups and NULL salience pass `1.0`. Note: formulas are *user*-authored and restricted-namespace `eval` is bypassable (`().__class__.__base__.__subclasses__()`), so a hostile formula means code execution — accepted for now (local single-user deployment), tracked as issue 001 in `plan/issue.md`.
5. Return the top-k hits across all types (`id`, `type`, `text`, score components, `salience`, `linked_count`).

This endpoint is what makes the other four changes observable: embedding A/B (qwen vs embeddinggemma-2 quality on the same queries), salience effect on ranking, and BM25 index correctness.

### 4.7 Admin API additions

```http
POST /admin/reembed
```

Response: counts of nulled vectors (messages/events/topics/facts). Idempotent; safe to call after any model switch.

---

## 5. API contract

### Search

```http
GET /chatgroups/{chatgroup_id}/search?q=trip%20to%20japan&k=20
```

Response:

```json
{
  "hits": [
    {
      "type": "topic",
      "id": "...",
      "full_text": "Camille from Lyon is interested in hiking, photography, and planning a trip to Japan.",
      "vector_score": 0.83,
      "bm25_score": 6.42,
      "salience": 0.9,
      "linked_count": 3,
      "score": 5.31
    }
  ]
}
```

### Re-embed

```http
POST /admin/reembed
```

```json
{"messages": 30, "events": 5, "topics": 1, "facts": 4}
```

---

## 6. Verification

```bash
# 1. Switch the embedding model in providers.yaml, then re-embed everything
curl -X POST http://localhost:8000/admin/reembed
curl -X POST http://localhost:8000/admin/batch/trigger

# 2. Salience: every message should get a real score
psql $VISTUI_DATABASE_URL -c \
  "SELECT processing_state->>'salience', count(*), avg(salience_score) FROM message GROUP BY 1;"

# 3. Event continuity: post new messages to an existing chat, trigger rounds, then
#    the LAST event should cover them (no new row while the conversation continues)
psql $VISTUI_DATABASE_URL -c \
  "SELECT full_text, start_at, end_at FROM event ORDER BY end_at DESC LIMIT 3;"

# 4. Protected window still intact: the newest 10 messages never consolidate
psql $VISTUI_DATABASE_URL -c \
  "SELECT count(*) FROM message WHERE processing_state->>'event' = 'waiting';"  # >= 10 while chat is idle

# 5. BM25 index sanity + hybrid search
psql $VISTUI_DATABASE_URL -c "SELECT * FROM paradedb_indexes();"
curl "http://localhost:8000/chatgroups/<id>/search?q=japan&k=10"

# 6. Full suites
just test               # unit: fakes cover salience, extension, decide/generate
just test-integration    # real models: salience scores, extension, search smoke
```

Unit/integration test updates:

- Fakes: `_fake_chat_model` learns the new prompts (decisions without summaries, salience scores, `event_decision` extend/new).
- New unit tests: salience pipeline (score + clamp + use_salience=false short-circuit), event extension (extend rewrites the last row, new creates rows; decision failure falls back to new), protected-window regression for extension, decide/generate split (create leaves salience NULL, update clears embedding).
- Integration test: assert real salience scores in [0, 1] on processed messages; assert the last event gets extended (not duplicated) when a continuing round runs; keep all existing queue-depth/window assertions.

---

## 7. Out of scope for Phase 4

- Retrieval quality tuning beyond the formula plumbing (prompt-based reranking, MMR…).
- Stale-event cleanup after message edits (edit → requeue → new/extended event, old event remains).
- LLM-computed `memory_ratio` (planned in earlier phases, still unimplemented; to be applied when the search gains its token budget — issue 002). Keywords extraction was removed on 2026-11-15: never wired (no caller, no storage, no consumer once BM25 indexes raw text).
- Retry logic for failed messages (still mark + count only).
- Ranking-formula editor UI / per-chatgroup search presets.
- Cross-ChatGroup consolidation and provider hot-reloading.

---

## 8. Changelog

- 2026-11-15: Created Phase 4 plan: embeddinggemma-2 switch, event continuity with protected window, ParadeDB BM25 indexes (drop `search_vector`), d3 salience scoring, decision/generation model split.
- 2026-11-15: Implemented 4.1 + 4.3 + 4.6 + 4.7 (embeddinggemma-2 switch and `POST /admin/reembed`, ParadeDB BM25 indexes replacing `search_vector`, hybrid search endpoint). 4.2, 4.4 and 4.5 are still pending. See §9.
- 2026-11-15: Ranking follow-up on search: default formula as a pydantic default, no hardcoded formulas, logarithmic `link_weight`, 0..1 normalization of both score channels. See §9.
- 2026-11-15: Code review of the whole uncommitted Phase 4 diff: BM25 query-token quoting (500 fix), NaN guard, reembed constant. See §9.
- 2026-11-15: Ranking-formula RCE: interpreter fix implemented and verified, then reverted by decision back to plain `eval`; documented as issue 001 in `plan/issue.md`. See §9.
- 2026-11-15: Middle step before 4.4/4.5: switched providers.yaml to the gomodel proxy (embedding via the `ollama_yople` upstream, LLM to `strata_toaster/iq3_xxs`), and validated the `d3:4b` decision model: it is System One-only (`/v1/systemone`, no chat completions) and gomodel cannot proxy that endpoint to ollama yet, so decisions go direct to `local-ollama`. See §9.
- 2026-11-15: Decision-model support in code: `defaults.decision` key in providers.yaml (local-ollama/d3:4b) and `Provider.decide()` — a System One client with typed `ChoiceQuestion`/`NoulQuestion`/`ScoreQuestion` models, `DecisionAnswer` parsing, keep_alive passthrough and 8 hermetic tests. See §9.
- 2026-11-15: Removed the never-wired `keywords` task (ChatGroupPrompts field, providers.yaml override, docs): no pipeline called it, nothing stored it, and BM25 indexes raw text. `memory_ratio` kept for the future search token-budget split (issue 002).
- 2026-11-15: Salience scoring switched from batched `decide()` calls (chunked at 5
  questions per chatgroup) to element-per-element — one call per message / memory
  object (user decision; simpler and faster, ~9s per element with d3:4b).
- 2026-11-15: Implemented 4.4 (salience with the decision model): `Provider.decide(task=...)` resolves decision tasks from the same overrides map, `SaliencePipeline` + `score_memory_objects` score messages and memory objects with d3:4b `ScoreQuestion` rubrics, opt-out chatgroups keep `salience_score` NULL (neutral 1.0 at scoring time — user decision, no fabricated placeholder), seeder/queue/gate updated, 9 new tests + integration assertions, `VISTUI_BATCH_INTERVAL_SECONDS=0` now disables the background scheduler (kills the pre-existing integration-test race). See §9.

---

## 9. Results

### Phase 1 — Embedding switch + re-embed (2026-11-15) — DONE

**Changes:** `providers.yaml` and the env-fallback in `llm/config.py` now use `embeddinggemma-2:740m`. New `POST /admin/reembed` nulls all vectors (messages/events/topics/facts) and requeues `processing_state.embedding = waiting` via `jsonb_set`, so the worker recomputes everything; returns per-table counts (`ReembedResponse`).

**Verification:** 61 unit tests pass (new `test_reembed_endpoint` covers null vectors + requeue). Full integration run against the real provider passed (seed → 2 worker rounds → all 30 messages embedded at 768-dim by embeddinggemma-2, events/topics/facts created). Total run ~291s vs ~304–409s previously — embedding stage went from ~39s (qwen, 30 texts) to ~1s; remaining time is dominated by the gemma4 chat calls.

**Notes:** switching back is a one-line `providers.yaml` change plus `POST /admin/reembed`. No code path depends on the model name; dimension validation in `Provider.embed` still enforces 768.

### Phase 2 — BM25 indexes + hybrid search endpoint (2026-11-15) — DONE

**Changes:** migration `0003_bm25_indexes` drops `search_vector` from message/event/topic/fact and creates `message_bm25_idx`, `event_bm25_idx`, `topic_bm25_idx`, `fact_bm25_idx` (`USING bm25 (id, full_text, chat_id|chatgroup_id) WITH (key_field='id')`); the models declare the same indexes so `create_all` in tests mirrors them. New `sources/backend/search/` package: `GET /api/v1/chatgroups/{chatgroup_id}/search?q=&k=`, hybrid BM25 + cosine fusion per memory type with `_CANDIDATE_LIMIT = 20` per mode.

**Verification:** `EXPLAIN ANALYZE` shows `Custom Scan (ParadeDB Base Scan)` on `message_bm25_idx` with `Exec Method: TopKScanExecState`, `TopK Order By: pdb.score() desc` and the `chat_id` filter pushed into the Tantivy query as a term — no post-index sort. Live calls against the real `embeddinggemma-2:740m` provider rank "My favourite hiking boots…" (bm25 5.65, vector 0.79) above "I went hiking in the mountains…" (bm25 3.49) above a semantically close but lexically absent "…balcony fence…" (bm25 0, vector 0.56). Nine search tests (`tests/backend/search/`) cover formula evaluation, the default formula, BM25 normalization, the link-weight curve, auth/404, `k` truncation and the full hybrid ranking; `alembic upgrade head` on the dev DB reaches `0003`.

**Notes:** the draft's `paradedb.all(full_text) & paradedb.parse(...)` is a no-op conjunction, so the implementation uses bare `parse()`. Multi-word queries are OR'd by ParadeDB's parser ("hiking boots" matches hiking-only rows). No vector index (HNSW/ivfflat) is created: the vector channel scans the chatgroup's rows with `ORDER BY cosine_distance(...) LIMIT 20`, fine for per-chatgroup result sets but worth revisiting at scale.

### Phase 3 — Salience with the decision model (2026-11-15) — DONE

**Changes:** `Provider.decide()` gained `task=` (decision tasks resolve from the same
`overrides:` map as chat tasks, `defaults.decision` as fallback; the `salience` override
points at `local-ollama`/`d3:4b`). New `backend/batch/salience.py` with two worker stages
inserted between the embedding stages and consolidation: `salience` (`SaliencePipeline`,
10 oldest waiting messages per round, one `decide()` call per message — element per
element, state = chatgroup description + the single message, a `ScoreQuestion` named
`salience` against a 5-level rubric, normalized score
`score / 4` clamped to [0,1]) and `memory_salience` (`score_memory_objects`, NULL-pickup
for events/topics/facts, up to 32 per round, one call per object). Opt-out
(`use_salience` false/null) marks the message stage done but leaves the score NULL — no
decision call, neutral 1.0 at scoring time. The event/topic/fact queries now enforce the
documented `salience = done` gate; topic/fact rewrites clear `salience_score` with the
embedding; the seeder queues salience like any other stage. `VISTUI_BATCH_INTERVAL_SECONDS=0`
disables the background scheduler (used by `just test-integration` to kill the
admin-trigger-vs-scheduler race).

**Verification:** 90 unit tests (9 new: decide task override, salience scoring,
limits, opt-out NULL, malformed-answer rollback, memory scoring, gate, queue count,
seeded state, worker round). Live end-to-end on the dev DB against real d3:4b: batched
first cut 10 messages in 2 chunked calls (246s) + 10 memory objects in 3 calls (160s);
element-per-element 4 messages in 4 calls (37s, ~9s each) — hiking plans/preferences
0.26–0.66, minor context ~0.2–0.4, stale placeholder texts 0.06–0.22. All hooks green
(ruff/format/flake8/bandit/mypy).

**Notes:** the original plan sketch (chat completion returning `{"salience": float}`)
predates the System One discovery — d3:4b has no chat API. Latency is the cost of the
probability-weighted rubric (~5-10s per scored element); element-per-element scoring was
chosen over batched calls after measuring superlinear batch growth (every question is
scored against the whole state) — a 10-message round costs ~90s instead of ~4 min, and
the 300s decision-provider timeout also covers the cold model load. A 30-message chat
drains in ~3 rounds (~5 min of d3 time). gomodel still cannot proxy `/v1/systemone`, so
decision calls stay direct to `local-ollama` (HTTP 400 "provider type ollama has no
System One API").

### Phase 3 — Ranking normalization and link weighting (2026-11-15) — DONE

Follow-up review of the search results; four fixes.

1. **`DEFAULT_RANKING_FORMULA` is a pydantic default on `ChatGroupBase`**, so `ChatGroupCreate` and `ChatGroupCreateInternal` inherit it and no caller has to pass a formula. The column deliberately keeps no DB server default.
2. **No hardcoded formulas**: `seeders.py` used its own vector-only formula (silently disabling BM25 for demo chatgroups) and `tests/backend/chatgroup/factories.py` duplicated the default string; both now import `DEFAULT_RANKING_FORMULA`.
3. **Logarithmic link weight**: multiplying by `linked_count` linearly scored every unconsolidated message at exactly 0 — invisible even on a perfect match. `search.scoring.link_weight` replaces it: 0 links → 0.5, 1 → 0.58, 10 → 0.76, 100 → 1.0 (`LINK_WEIGHT_REFERENCE`), logarithmic throughout and still slowly growing past the reference. Exposed to admin formulas as `link_weight(linked_count)`, next to `max`, `min`, `abs`, `log`, `log10`, `sqrt`.
4. **Normalization**: BM25 is divided by the best BM25 hit of the query (`normalize_bm25`) and the vector channel is clamped, both before the formula runs, so the channels are comparable and `score` is reproducible from the reported components.

Default formula: `lambda vector_score, bm25_score, salience, linked_count: max(vector_score, bm25_score) * salience * link_weight(linked_count)`

**Verification:** 70 unit tests pass (`just test`), covering the inherited pydantic default, formula evaluation with helper functions, `normalize_bm25`, the link-weight curve, and the hybrid endpoint end to end (normalized channels, deterministic ranking: consolidated messages first, plain messages next, then the placeholder-salience event). Live check on the dev DB with real embeddings: an unconsolidated perfect BM25 match now scores 0.500 instead of 0.000, and `q="balcony fence"` still ranks the fence message first (bm25 1.0) with the hiking messages behind it on vector score alone. `ruff` and `mypy` clean.

### Phase 4 — Code review of the uncommitted diff (2026-11-15) — FIXED

Review of everything not yet committed (admin/reembed, BM25 migration, models, search package, seeding, chatgroup schemas). Four findings, all fixed:

1. **Critical — arbitrary code execution via `ranking_formula`.** Any authenticated user can set a chatgroup's formula (`POST`/`PATCH /api/v1/chatgroups`), and search `eval`s it with `{"__builtins__": {}}` — which is bypassable without builtins: `().__class__.__base__.__subclasses__()` reaches every loaded class (verified: 228 reachable), and from there it is a hop to `subprocess`. An interpreter fix (AST whitelist + `literal_eval`-style node-by-node evaluation, no `eval`/`compile`, plus 422-on-write) was implemented and fully verified, then **reverted by decision** to keep the simple `eval` for this local single-user deployment. The vulnerability stays open and is documented with the full fix design in `plan/issue.md` (issue 001).
2. **High — Lucene injection on the search endpoint.** The raw `q` reached ParadeDB's query-string parser: `a"b`, `field:value`, `boots AND (x OR y)`, `h~2`, `\`, `-`, a bare `NOT` and whitespace-only queries all raised `could not parse query string` → HTTP 500. Fix: `_quote_bm25_tokens` phrase-quotes every whitespace-separated token (inner quotes/backslashes escaped), the API adds `pattern=r"\S"` to reject blank queries, and the service early-returns on `q.strip() == ""`. Recall is unchanged: the BM25 analyzer normalizes quoted tokens the same way (`"hiking!"` still matches 9/9 hiking rows, multi-token queries still OR).
3. **Medium — NaN propagation.** A zero/NaN embedding makes `cosine_distance` return NaN, which survived `clamp_unit` (Python `min`/`max` propagate NaN) and poisoned scores and sort order. Fix: `clamp_unit` maps non-finite values to 0.0.
4. **Cosmetic — reembed hardcoded the `"waiting"` string** instead of `ProcessingStatus.WAITING`; now it uses the constant (`json.dumps`, since `jsonb_set` takes a JSONB literal).

Verified non-issues: search ownership filtering (`ChatGroupService.retrieve` scopes by user), reembed→worker requeue contract (`embed_memory_objects` picks up NULL-embedding events/topics/facts), migration downgrade fidelity (0002 never created the dropped GIN indexes), `Table.append_constraint(Index)` is officially supported, `k`/`q` bounds, and the formula compilation cache (bounded by chatgroup count).

**Verification:** 73 unit tests pass (`just test`) — coverage includes formula evaluation (default, math helpers, boolean/conditional semantics, non-lambda rejection), hostile BM25 queries returning 200 with non-NaN scores, blank queries 422, and a custom formula accepted on chatgroup create (201). Live dev-DB checks: hostile queries return vector-only hits instead of 500s; end-to-end search unchanged with the plain `eval`. `ruff` and `mypy` clean.

---

### Middle step — gomodel proxy + q3 LLM (2026-11-15) — DONE

Prep before 4.4/4.5: move both LLM and embedding traffic onto the gomodel proxy and swap the
generation model to the q3 quant, so the decision/generation split can be configured purely in
`providers.yaml`.

- `providers.yaml`: new `gomodel` provider (`https://gomodel.eagal.org/v1`, key from `GOMODEL_TOKEN`). Defaults: embedding `ollama_yople/embeddinggemma-2:740m` (unchanged model, now via the proxy), LLM `strata_toaster/iq3_xxs` (instead of `gemma4:e4b`). The `overrides:` block was initially dropped (every entry duplicated the old default and would have pinned tasks to gemma), then restored per user request as the explicit per-task tuning surface — all entries set to `gomodel/strata_toaster/iq3_xxs`, i.e. identical to the default; 4.5 will split them per task. `local-ollama` stays declared for direct access / rollback.
- The proxy's model namespace is upstream-prefixed: `ollama_yople/*` is our own ollama box (the same one `local-ollama` points at), `strata_toaster/iq3_xxs` is a model on a PC that is not directly reachable — a direct-to-ollama q3 call is impossible from here, so all q3 validation goes through gomodel.

**Verification:** layer by layer —
1. Direct ollama baseline (gemma chat, embedding) on `192.168.9.137`.
2. Same calls through gomodel's `ollama_yople` upstream: chat answers identical, embeddings **bit-identical** (cosine 1.0, 768 dims) — the proxy is transparent.
3. `strata_toaster/iq3_xxs` through gomodel: simple chat ~2.4s, keyword-extraction JSON task clean (~5s).
4. Through the app's `Provider` (langchain path incl. `response_format=json_object`): embed 768d (0.4s), chat 'OK' (0.7s), `chat_json` parses (2.1s).
`just test` still 73 passed (providers are mocked in the unit suite).

### Middle step, part 2 — the d3 decision model (2026-11-15) — DONE

Correcting an earlier misreading: the model to check was `d3:4b` on the yople ollama (the
decision model), not the strata quant. Validated direct-first, then through the proxy:

- `d3:4b` is **not a chat model.** `/v1/chat/completions` rejects it identically on ollama
  and through gomodel: `"d3:4b" does not support chat`. `/api/show` reports capabilities
  `["decision", "vision"]`, `qwen35.decision.type = vllm-d3` (Qwen3.5 4B base); the server
  runs ollama 0.40.16.
- It is called with **`POST /v1/systemone`** (ollama ≥ 0.35): one shared `state` (text, JSON
  or image) plus 1–64 typed `questions`. Types: `choice` (label → probabilities),
  `noul` (yes/no probability), `score` (2–10 ordered rubric levels → probability-weighted
  index + confidence). Answers are probabilities/scores only — never conversational text.
- **Direct (192.168.9.137): works.** State = the demo hiking message, 3 questions:
  salience `score` (5-level rubric) → 1.667/4 (conf 0.21); keep `noul` → P(true) 0.19;
  mood `choice` → positive p 0.97 (conf 0.88). Sensible on all three. 23.3s total,
  383 input tokens, 0 output tokens (questions are scored as independent passes).
- **Through gomodel: blocked.** `POST https://gomodel.eagal.org/v1/systemone` with
  `ollama_yople/d3:4b` → 400: *"model is served by provider type ollama, which has no
  System One API; /v1/systemone forwards requests natively and does not translate them to
  other APIs"*. The claim is outdated for ollama ≥ 0.35, but it is what the proxy enforces
  (it only knows jev / OpenRouter / Azure decision models). Until gomodel learns to forward
  systemone to ollama upstreams, decision calls must use the `local-ollama` provider
  directly; embedding and chat stay on gomodel.

**Design impact on 4.2/4.4/4.5** (apply when implementing):

1. The planned chat-task overrides (`salience`, `topic_candidate`, `fact_candidate`,
   `event_decision` → `d3:4b`) cannot work: ChatOpenAI speaks chat completions, which d3
   rejects. The Provider needs a System One client — a `decide(state, questions)` method
   hitting `/v1/systemone` — with its own provider config section (provider `local-ollama`,
   model `d3:4b` for now).
2. Salience 0.0–1.0: a `score` question with a ~5-level rubric, normalized by levels − 1
   (or a `noul` probability). Rubric wording to be tuned at implementation.
3. Decision prompts become typed questions: extend-vs-new is a `choice` over
   `{extend, new}` with criteria descriptions; create/update/skip similarly. Probabilities
   and confidence come for free and can drive fallback thresholds.
4. Latency: ~23s for 3 questions on the current box — each question is an independent
   scoring pass. Batch stages should ask few questions per message and lean on keep_alive.

### Middle step, part 3 — decision call support in code (2026-11-15) — DONE

The plumbing 4.2/4.4/4.5 will call, landed ahead of them:

- `providers.yaml` gains `defaults.decision` (new key): provider `local-ollama`, model
  `d3:4b` — direct, because gomodel cannot forward `/v1/systemone` (see part 2).
  `backend.llm.config` gets `DecisionDefaults` (+ legacy env fallback
  `VISTUI_DECISION_MODEL`, default `d3:4b`).
- `Provider.decide(state, questions, keep_alive=None)` in `backend.llm.provider`: typed
  question models `ChoiceQuestion` / `NoulQuestion` / `ScoreQuestion` (client-side mirrors
  of the server limits: 2–255 options, false/true criteria only, 2–10 rubric levels), a
  `System One` HTTP client (`POST {base_url}/systemone` via httpx, provider timeout and
  api_key_env honored), `DecisionAnswer` parsing with per-question validation, missing-answer
  detection, HTTP/network errors surfaced as `DecisionError`, usage logging, and optional
  `keep_alive` for batch rounds.
- 8 hermetic tests (`tests/backend/llm/test_provider.py`): config fallback, payload shape
  (URL, auth header, exclude_none serialization), keep_alive passthrough, empty/missing
  answers, HTTP 400 and network errors, question model limits. 81 unit tests total.

**Verification:** live end-to-end through `Provider.decide()` with the real providers.yaml
against the box: 3 questions in 16.1s — salience `score` 1.667/4 (conf 0.21), `keep` noul
0.26, `mood` choice positive p 0.97; matches the raw-curl validation. `ruff`/`mypy` clean.
