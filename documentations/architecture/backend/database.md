# Vistui Database

Guidelines for database design and SQLAlchemy usage in Vistui.

## Connection Setup

Configured in `sources/backend/database.py`:

- **Driver**: `postgresql+psycopg_async`
- **Pool size**: 10 connections
- **Max overflow**: 10 additional connections
- **Pool timeout**: 30 seconds
- **Pool recycle**: 1800 seconds (30 minutes, before PostgreSQL's 1h limit)

## Required Extensions

| Extension | Purpose |
|-----------|---------|
| `pgvector` | Vector storage and similarity search. |
| `pg_search` (ParadeDB) | BM25 full-text search. |

These are provided by the recommended ParadeDB Docker image.

## Model Design Rules

### Naming

- Table names: lowercase singular (e.g., `message`, not `messages`).
- Model classes: `<Feature>DO` suffix (e.g., `MessageDO`, `ChatGroupDO`).
- Primary keys: `id` column, UUID type.

### Primary Keys

- Most entities use database-generated UUIDs.
- `MessageDO.id` is caller-provided to support idempotent `PUT` semantics.
- Always indexed: `mapped_column(Uuid, primary_key=True, index=True)`.

### Relationships

- Use `back_populates` for bidirectional access.
- Define relationships on both sides when possible.
- Foreign keys are `nullable=False` by default unless explicitly optional.
- `Chat.chatgroup_id` is immutable after creation.

### Column Types

- Strings: use explicit `String(length)` with reasonable limits.
- Text: use `Text` for message content, summaries, and facts.
- JSON data: use `JSONB` for `config`, `prompts`, and `processing_state`.
- Vectors: use `pgvector` `Vector(dimensions)` for embeddings.
- Full-text: use `TSVectorType` or `tsvector` for `search_vector`.
- Dates: use `DateTime(timezone=True)` for timestamps.
- Status/processing fields: stored as `String` or `JSONB` and validated via schemas.

## Indexes

| Column(s) | Index type | Purpose |
|-----------|------------|---------|
| `embedding` (messages, events, topics, facts) | HNSW | Fast approximate nearest neighbor search. |
| `search_vector` (messages, events, topics, facts) | GIN | BM25 / full-text search. |
| `chat_id`, `chatgroup_id` | B-tree | Foreign key lookups and joins. |
| `processing_state` flags | B-tree / GIN on JSONB keys | Queue queries for the batch worker. |

## Session Management

### `with_async_session` Decorator

Applied to all store methods. Handles session lifecycle automatically:

- Creates a new session with transaction management when called without a session.
- Reuses an existing session when one is passed (allows nested transactions).

### Transaction Boundaries

- Each store method is a transaction boundary by default.
- Services compose multiple store calls; each call is its own transaction unless a session is passed explicitly.
- For multi-store atomic operations, pass a session explicitly.

## Migrations

- Managed with Alembic.
- Run automatically on container startup before the app starts serving requests.
- A separate init container option is documented for production-like deployments.

## Rules Summary

| Rule | Rationale |
|------|-----------|
| One store writes to one model | Prevents tight coupling, enables reuse. |
| Use database-generated UUIDs for most PKs | Simple, predictable identity. |
| Caller-provided UUID for messages | Enables idempotent `PUT` creation and editing. |
| `back_populates` on relationships | Bidirectional navigation, consistency. |
| `nullable=False` by default | Explicit opt-in for nullable fields. |
| `JSONB` for structured data | PostgreSQL-native, indexable, efficient. |
| HNSW for vector indexes | Fast ANN at scale. |
