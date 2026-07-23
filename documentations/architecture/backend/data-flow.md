# Vistui Data Flow

How requests flow through the Vistui backend.

## Request Lifecycle

```
HTTP Request
    |
    v
FastAPI Router (apis.py)
    |
    v
External Schema Validation (schemas.py - external)
    |
    v
[API Layer] Convert to Internal Schema
    - Inject chat_id, message_id, user_id
    - Add context (retrieve flag, user_id)
    |
    v
Service Layer (services.py)
    - Validate linked-list integrity
    - Trigger embedding / keyword / salience computation
    - Coordinate retrieval
    |
    v
Store Layer (stores.py)
    - CRUD operations
    - Query execution
    |
    v
Database (database.py)
    - SQLAlchemy ORM
    - PostgreSQL + pgvector + pg_search
    |
    v
Response flows back:
    DB -> Store returns Internal Schema
    Store -> Service returns Internal Schema
    Service -> API returns Internal Schema
    API -> Router converts to Read / MemoryResponse Schema
    Router -> Client receives External Schema
```

## Layer Boundaries

### API Layer Rules

- Receive **external schemas** (`Create`, `Update`, `Filter`) from the client.
- Convert to **internal schemas** (`CreateInternal`, `UpdateInternal`) before calling services.
- Inject ownership/permission data and request flags (e.g., `retrieve`).
- Return **Read schemas** and retrieval-specific response schemas to the client.
- May call multiple services only as a last resort.

Example — injecting `chat_id` and `id`:

```python
message_internal = MessageCreateInternal.model_validate(
    {**message.model_dump(exclude_unset=True), "chat_id": chat_id, "id": message_id}
)
```

Example — adding `context`:

```python
class MessageSaveContext(BaseModel):
    retrieve: bool
    user_id: uuid.UUID

class MessageCreateInternal(MessageCreate):
    context: MessageSaveContext
```

### Service Layer Rules

- May call **multiple stores** to compose data operations.
- **Never calls other services**; extract shared logic to business functions instead.
- Validates linked-list integrity, ChatGroup scoping, and processing state transitions.
- Returns internal schemas.

### Store Layer Rules

- Writes to **only one model** per store.
- Specialized read classes (`QueryStore`) may read from multiple models.
- All methods are `@staticmethod` + `@with_async_session`.
- Handles basic `IntegrityError` via `handle_foreign_key_violation`.

## Context Pattern

`context` in schemas holds data needed by services for business logic but **not needed by stores** for CRUD:

- `MessageSaveContext` carries `retrieve` and `user_id`.
- `ChatCreateContext` carries `auto_create_chatgroup` behavior.
- `ChatGroupUpdateContext` carries `user_id` for ownership checks.

Stores ignore `context` fields via `model_dump(exclude={"context"})`.

## Session Management

The `@with_async_session` decorator in `database.py` handles session lifecycle:

- If a `session: AsyncSession` argument is already provided, uses it directly.
- Otherwise, creates a new session with transaction management.
- This allows stores to participate in parent transactions when needed.

```python
@with_async_session
async def create(session: AsyncSession, ...) -> ...:
    # session is managed automatically
    ...
```

## Retrieval Flow

The `PUT /chats/{chat_id}/messages/{message_id}?retrieve=true` path has an extended flow:

1. Save or overwrite the message in the database.
2. Compute embedding, keywords, and salience in parallel.
3. Optionally compute a memory ratio via LLM or use the ChatGroup default.
4. Run vector and BM25 searches across Messages, Events, Topics, and Facts.
5. Normalize scores and rank candidates with the ChatGroup `ranking_formula`.
6. Pack results into the token budget.
7. Return a `MemoryResponse` block.
