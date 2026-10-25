# Vistui Phase 3: Embeddings + Batch Consolidation

**Goal:** Add a local Ollama LLM provider, compute embeddings for messages, seed a fake chat with 30 messages, and run an in-process batch worker that consolidates older messages into Events, Topics, and Facts while also creating embeddings for those consolidated objects.

**Scope focus:** "embeddings alone for Phase 3" — salience scoring is intentionally out of scope. Salience will be short-circuited (set to `done` with a neutral placeholder) so that event/topic/fact pipelines can run without a real salience model.

---

## 1. Decisions

| Topic | Decision |
|-------|----------|
| LLM backend | Ollama OpenAI-compatible API at `http://192.168.9.137:11434/v1` |
| Embedding model | `qwen3-embedding:8b` → 768-dim vectors (matches existing schema) |
| Grouping / chat model | `gemma4:e4b` used for message grouping and all consolidation prompts |
| Vector column type | Replace `pgvector.sqlalchemy.VECTOR` with `paradedb.sqlalchemy.vector.Vector(768)` |
| Batch scheduler | APScheduler `AsyncIOScheduler` with `SQLAlchemyJobStore` (in-process, single worker) |
| Demo data | `seed_demo_chat()` creates one ChatGroup, one Chat, and 30 messages. Triggered via `POST /admin/seed/demo` or at startup when `VISTUI_SEED_DEMO=1` |
| Manual batch trigger | `POST /admin/batch/trigger` runs one worker round synchronously |
| Status | `GET /admin/batch/status` returns running flag, current pipeline, queue depth, recent failure count |
| Failure handling | Mark sub-task `failed`, increment retry count, log the exception; no retry logic in Phase 3 |
| Protected window | `ChatGroup.config.protected_message_count` most recent messages are skipped by consolidation |

---

## 2. Dependencies

Already added with `uv add`:

- `sqlalchemy-paradedb>=0.13.0`
- `openai>=3.35.1`
- `apscheduler>=3.11.3`

`pgvector` remains because ParadeDB still relies on the pgvector extension for the `vector` type.

---

## 3. File plan

### New directories/files

```
sources/backend/llm/
  __init__.py
  config.py          # environment-based provider settings
  provider.py        # AsyncOpenAI wrapper: embed() + chat()
  prompts.py         # default consolidation prompts used when ChatGroup prompts are absent

sources/backend/batch/
  __init__.py
  embeddings.py      # embed_messages() and embed_memory_objects()
  worker.py          # BatchWorker: single round, should_stop flag
  pipelines.py       # EventPipeline, TopicPipeline, FactPipeline
  scheduler.py       # AsyncIOScheduler setup + SQLAlchemyJobStore
  status.py          # in-memory worker status dataclass
  queries.py         # helper queries: next waiting message, protected window, etc.

sources/backend/admin/
  __init__.py
  apis.py            # /admin/seed/demo, /admin/batch/trigger, /admin/batch/status
  schemas.py         # status response models
```

### Modified files

```
pyproject.toml                         # done — dependencies added
sources/backend/settings.py            # add Ollama env vars and seed flag
sources/backend/database.py            # expose scheduler-friendly engine/session factory
sources/backend/main.py                # start scheduler, register admin router
sources/backend/seeders.py             # add seed_demo_chat()
sources/backend/message/models.py      # VECTOR -> Vector
sources/backend/event/models.py        # VECTOR -> Vector
sources/backend/topic/models.py        # VECTOR -> Vector
sources/backend/fact/models.py         # VECTOR -> Vector
migrations/versions/0002_create_core_tables.py  # VECTOR -> Vector
tests/backend/config.py                # type mapping: pgvector VECTOR -> paradedb Vector
```

---

## 4. Implementation steps

### 4.1 Vector type switch

- Replace `from pgvector.sqlalchemy import VECTOR` with `from paradedb.sqlalchemy.vector import Vector`.
- Replace `VECTOR(768)` with `Vector(768)` in models and migration.
- Update test factory type mapping.

### 4.2 LLM provider module

`backend/llm/config.py`:

```python
class LLMSettings(BaseSettings):
    VISTUI_LLM_BASE_URL: str = "http://192.168.9.137:11434/v1"
    VISTUI_LLM_API_KEY: SecretStr | None = None
    VISTUI_EMBEDDING_MODEL: str = "qwen3-embedding:8b"
    VISTUI_CHAT_MODEL: str = "gemma4:e4b"
    VISTUI_EMBEDDING_DIMENSIONS: int = 768
    VISTUI_LLM_TIMEOUT_SECONDS: int = 120
```

`backend/llm/provider.py`:

- `Provider` wraps `AsyncOpenAI`.
- `async def embed(texts: list[str]) -> list[list[float]]`: validates dimension, raises `EmbeddingError`.
- `async def chat(messages: list[dict[str, str]], temperature: float = 0.2, json_mode: bool = False) -> str`.
- Optional structured output helper `chat_json()`.

### 4.3 Message embedding pipeline

`backend/batch/embeddings.py`:

- `embed_messages(session, limit=32)`:
  1. Select oldest messages where `processing_state.embedding == 'waiting'`.
  2. Batch texts and call `provider.embed()`.
  3. Update each message: `embedding = vector`, `processing_state.embedding = 'done'`.
  4. Commit once per batch.
- `embed_memory_objects(session, limit=32)`:
  1. Select oldest Event/Topic/Fact where `embedding IS NULL`.
  2. Compute embeddings, update, mark state if applicable.

### 4.4 Demo chat seeder

`backend/seeders.py` → `seed_demo_chat()`:

- Create ChatGroup named `"Phase 3 Demo"` for the seeded default user if not exists.
- Create Chat named `"Demo Chat"`.
- Insert 30 messages (mix of `user` and `assistant`) about: travel plans, work projects, family updates, hobbies, reminders.
- Messages have `processing_state` all `waiting` and no embedding.

`backend/admin/apis.py` endpoint `POST /admin/seed/demo` calls it.

### 4.5 Consolidation pipelines

All pipelines select the **oldest waiting message** outside the protected window with `embedding = done`. Salience is short-circuited.

#### Event pipeline

1. Load the target message plus contiguous subsequent messages that are also `event = waiting` and outside the protected window, up to `max_event_batch_size` (10).
2. Call `provider.chat_json()` with a prompt asking the model to group the block into one or more events.
   - Expected response: list of `{"summary": "...", "message_ids": [id1, id2, ...]}`.
3. For each returned event:
   - Create `EventDO` with `full_text = summary`, `start_at/end_at` from linked messages.
   - Link the listed messages.
   - Compute embedding via `embed_memory_objects` or inline provider call.
4. Mark all processed messages `event = done`.

#### Topic pipeline

1. For the target message, find top-K existing topics in the same ChatGroup by cosine distance on `embedding`.
2. Call `provider.chat_json()` with the message text and topic summaries. Ask: create new topic(s), update existing topic(s), or skip.
   - Response: `[{"action": "create|update|skip", "topic_id": "..."|null, "summary": "..."}, ...]`.
3. For creates: insert `TopicDO`, link message, compute embedding.
4. For updates: rewrite `TopicDO.full_text`, clear `embedding` to null, link message; embedding job will fill it later.
5. Mark message `topic = done`.

#### Fact pipeline

Same shape as topic pipeline but scoped to `FactDO` and with a fact-specific prompt.

### 4.6 Worker / scheduler

`backend/batch/worker.py`:

- `BatchWorker` owns a `should_stop: bool` flag and runs rounds sequentially.
- One round:
  1. `embed_messages()`.
  2. `embed_memory_objects()`.
  3. `EventPipeline.run_round()`.
  4. `TopicPipeline.run_round()`.
  5. `FactPipeline.run_round()`.
- Check `should_stop` between sub-tasks.

`backend/batch/scheduler.py`:

- Build `AsyncIOScheduler` with `SQLAlchemyJobStore(engine=engine, tablename="apscheduler_jobs")`.
- Add a job that runs the worker round every `batch_inactivity_seconds` from config (default 3600). For Phase 3 the interval is acceptable because the worker is cheap to skip when empty.
- Start scheduler in `main.py` lifespan.

`backend/batch/status.py`:

- Simple in-memory dataclass: `running`, `current_pipeline`, `current_message_id`, `queue_depth`, `recent_failures`.
- Worker updates it as it goes.

### 4.7 Admin API

`backend/admin/apis.py`:

- `POST /admin/seed/demo` — idempotent demo chat seeding.
- `POST /admin/batch/trigger` — run one worker round and return status.
- `GET /admin/batch/status` — return current status.

No auth on these endpoints for Phase 3 (dev convenience). Protect with `get_current_active_user` if desired.

### 4.8 App wiring

`backend/main.py`:

- In lifespan:
  1. `create_db_and_tables()`.
  2. `create_default_user()`.
  3. `start_scheduler()`.
  4. If `VISTUI_SEED_DEMO=1`, `seed_demo_chat()`.
- Include `admin.router`.

---

## 5. API contract

### Seed demo

```http
POST /admin/seed/demo
```

Response:

```json
{
  "chatgroup_id": "...",
  "chat_id": "...",
  "message_count": 30
}
```

### Trigger batch

```http
POST /admin/batch/trigger
```

Response:

```json
{
  "status": "ok",
  "ran_pipeline": true
}
```

### Batch status

```http
GET /admin/batch/status
```

Response:

```json
{
  "running": false,
  "current_pipeline": null,
  "current_message_id": null,
  "queue_depth": 42,
  "recent_failures": 0
}
```

---

## 6. Verification

```bash
# 1. Start ParadeDB
just up

# 2. Apply migrations
just migrate

# 3. Configure Ollama (or override in .env)
export VISTUI_LLM_BASE_URL="http://192.168.9.137:11434/v1"
export VISTUI_SEED_DEMO=1

# 4. Run server
just run

# 5. Seed demo chat and trigger batch manually
curl -X POST http://localhost:8000/admin/seed/demo
curl -X POST http://localhost:8000/admin/batch/trigger

# 6. Check status and inspect DB
just shell
psql $VISTUI_DATABASE_URL -c "SELECT id, processing_state->>'embedding', embedding IS NOT NULL FROM message;"
```

---

## 7. Out of scope for Phase 3

- Real salience scoring.
- BM25 / hybrid retrieval endpoints.
- Ranking formula evaluation.
- Reprocessing edited messages.
- Cross-ChatGroup consolidation.
- Provider YAML hot-reloading.

---

## 8. Changelog

- 2026-10-06: Created Phase 3 plan focusing on embeddings, Ollama integration, demo chat, and batch consolidation.
