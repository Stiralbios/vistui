# Vistui Core CRUD Implementation Plan

**Date:** 2026-07-25
**Goal:** Implement simple, full CRUD for the Vistui memory domain, inspired by Accurial's layered architecture but kept intentionally minimal.

---

## 1. Scope

Implement CRUD for all core entities:

- `User` (add authenticated `POST /users`)
- `ChatGroup`
- `Chat`
- `Message`
- `Event`
- `Topic`
- `Fact`

Out of scope for this pass:

- LLM calls / embeddings computation / batch worker
- Retrieval / ranking / memory responses
- Full linked-list validation / cycle detection
- Soft deletes

---

## 2. Guiding Principles

- Follow the documented Vistui feature-based folder layout: `models.py`, `schemas.py`, `stores.py`, `services.py`, `apis.py`.
- Mirror Accurial's strict **internal/external schema split** with a `context` object for service-layer authorization.
- Stateless `*Store` classes using `@with_async_session`.
- Services may call multiple stores but never other services.
- APIs convert external schemas to internal schemas and inject `user_id` / `chat_id` / `chatgroup_id` / `id` / `context`.
- Table names are lowercase singular.
- All mutating endpoints require the existing JWT `get_current_active_user` dependency.
- Server-generated UUIDs for all resources except Message `id` on PATCH (from URL).

---

## 3. Dependencies

Add to `pyproject.toml`:

```toml
"pgvector",
"sqlalchemy-pgvector-utils",  # or the appropriate SQLAlchemy pgvector helper
```

Also enable the `vector` extension in the migration and in dev/test database setup.

---

## 4. Exceptions

Add new `Entity` members and concrete `BaseProblem` subclasses in `sources/backend/exceptions.py`:

```python
class Entity(StrEnum):
    USER = auto()
    CHATGROUP = auto()
    CHAT = auto()
    MESSAGE = auto()
    EVENT = auto()
    TOPIC = auto()
    FACT = auto()
```

Each entity gets:

- `*NotFoundProblem` (404)
- `*AlreadyExistProblem` (409) where relevant
- `*NotAllowedProblem` (403) for ownership violations
- For join links: `EventMessageLinkAlreadyExistProblem`, etc.

---

## 5. Database Models

### 5.1 ChatGroup

```python
class ChatGroupDO(Base):
    __tablename__ = "chatgroup"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    prompts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ranking_formula: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
```

### 5.2 Chat

```python
class ChatDO(Base):
    __tablename__ = "chat"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chatgroup_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chatgroup.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
```

### 5.3 Message

Only `prev_message_id` in the database. `next_message_id` is intentionally omitted.

```python
class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"

class MessageDO(Base):
    __tablename__ = "message"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat.id"), nullable=False)
    prev_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message.id"), nullable=True)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[Any] = mapped_column(TSVectorType())
    embedding: Mapped[Vector | None] = mapped_column(Vector(768), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    salience_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    processing_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
```

### 5.4 Event

```python
class EventDO(Base):
    __tablename__ = "event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat.id"), nullable=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[Any] = mapped_column(TSVectorType())
    embedding: Mapped[Vector | None] = mapped_column(Vector(768), nullable=True)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    salience_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
```

### 5.5 Topic

```python
class TopicDO(Base):
    __tablename__ = "topic"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chatgroup_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chatgroup.id"), nullable=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[Any] = mapped_column(TSVectorType())
    embedding: Mapped[Vector | None] = mapped_column(Vector(768), nullable=True)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_edited_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    salience_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
```

### 5.6 Fact

Same shape as Topic, scoped to `chatgroup_id`.

### 5.7 Join Tables

Composite primary keys prevent duplicates at the DB level:

```python
class EventMessageLinkDO(Base):
    __tablename__ = "event_message_links"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("event.id"), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message.id"), primary_key=True)

class TopicMessageLinkDO(Base):
    __tablename__ = "topic_message_links"

    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topic.id"), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message.id"), primary_key=True)

class FactMessageLinkDO(Base):
    __tablename__ = "fact_message_links"

    fact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fact.id"), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message.id"), primary_key=True)
```

---

## 6. Schemas

### 6.1 Internal/External Split

For every entity follow this naming convention:

| Schema | Purpose |
|--------|---------|
| `FeatureBase` | Shared fields |
| `FeatureRead` | External response |
| `FeatureCreate` | External POST body |
| `FeatureCreateInternal` | Internal create with injected `user_id`/`chat_id`/`chatgroup_id` and `context` |
| `FeatureUpdate` | External PATCH body |
| `FeatureUpdateInternal` | Internal update with injected `id`, parent ids, and `context` |
| `FeatureContext` | Operation metadata passed to services |
| `FeatureFilter` | Query parameters for list endpoints |

### 6.2 ChatGroup

```python
class MemoryRatio(BaseModel):
    message: float = 0.40
    event: float = 0.10
    topic: float = 0.30
    fact: float = 0.20

class ChatGroupConfig(BaseModel):
    token_budget: int = 2000
    memory_ratio: MemoryRatio = Field(default_factory=MemoryRatio)
    use_llm_memory_ratio: bool = False
    use_salience: bool = True
    protected_message_count: int = 10
    topic_update_threshold: float = 0.75
    fact_update_threshold: float = 0.80
    fact_history_count: int = 5
    max_facts_per_message: int = 5
    max_topics_per_message: int = 5
    batch_inactivity_seconds: int = 3600
    processing_timeout_seconds: int = 3600

class ChatGroupPrompts(BaseModel):
    salience: str = "..."
    keywords: str = "..."
    memory_ratio: str = "..."
    event: str = "..."
    topic_candidate: str = "..."
    topic_update: str = "..."
    fact_candidate: str = "..."
    fact_update: str = "..."

class ChatGroupBase(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=1024)

class ChatGroupRead(ChatGroupBase):
    id: uuid.UUID
    user_id: uuid.UUID
    config: ChatGroupConfig
    prompts: ChatGroupPrompts
    ranking_formula: str
    created_at: dt.datetime
    updated_at: dt.datetime | None

class ChatGroupCreate(ChatGroupBase):
    config: ChatGroupConfig = Field(default_factory=ChatGroupConfig)
    prompts: ChatGroupPrompts = Field(default_factory=ChatGroupPrompts)
    ranking_formula: str = DEFAULT_RANKING_FORMULA

class ChatGroupContext(BaseModel):
    user_id: uuid.UUID

class ChatGroupCreateInternal(ChatGroupCreate):
    user_id: uuid.UUID
    context: ChatGroupContext

class ChatGroupUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    config: ChatGroupConfig | None = None
    prompts: ChatGroupPrompts | None = None
    ranking_formula: str | None = None

class ChatGroupUpdateInternal(ChatGroupUpdate):
    id: uuid.UUID
    context: ChatGroupContext

class ChatGroupFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
```

### 6.3 Chat

```python
class ChatBase(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=1024)

class ChatRead(ChatBase):
    id: uuid.UUID
    chatgroup_id: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime | None

class ChatCreate(ChatBase):
    chatgroup_id: uuid.UUID | None = None

class ChatContext(BaseModel):
    user_id: uuid.UUID

class ChatCreateInternal(ChatCreate):
    context: ChatContext

class ChatUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1024)

class ChatUpdateInternal(ChatUpdate):
    id: uuid.UUID
    context: ChatContext

class ChatFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
```

### 6.4 Message

```python
class MessageBase(BaseModel):
    full_text: str = Field(max_length=32000)
    role: MessageRole

class MessageRead(MessageBase):
    id: uuid.UUID
    chat_id: uuid.UUID
    prev_message_id: uuid.UUID | None
    keywords: list[str] | None
    salience_score: float | None
    processing_state: dict
    created_at: dt.datetime
    updated_at: dt.datetime | None

class MessageCreate(MessageBase):
    model_config = ConfigDict(extra="forbid")
    # caller does NOT provide prev_message_id

class MessageContext(BaseModel):
    user_id: uuid.UUID

class MessageCreateInternal(MessageCreate):
    id: uuid.UUID
    chat_id: uuid.UUID
    prev_message_id: uuid.UUID | None
    processing_state: dict
    context: MessageContext

class MessageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_text: str | None = Field(default=None, max_length=32000)
    role: MessageRole | None = None

class MessageUpdateInternal(MessageUpdate):
    id: uuid.UUID
    chat_id: uuid.UUID
    context: MessageContext

class MessageFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
    role: MessageRole | None = None
```

### 6.5 Event / Topic / Fact

Follow the same pattern:

- `FeatureBase`
- `FeatureRead`
- `FeatureCreate`
- `FeatureCreateInternal` with parent id (`chat_id` or `chatgroup_id`) + `context`
- `FeatureUpdate`
- `FeatureUpdateInternal` with `id` + `context`
- `FeatureFilter`

---

## 7. Cursor Pagination Envelope

```python
from typing import Generic, TypeVar

T = TypeVar("T")

class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: uuid.UUID | None
    has_more: bool
```

All list endpoints return `CursorPage[FeatureRead]`.

---

## 8. Stores

Stateless classes with `@with_async_session` methods.

### Common store interface per entity

```python
class FeatureStore:
    @staticmethod
    @with_async_session
    async def create(session: AsyncSession, obj: FeatureCreateInternal) -> FeatureInternal: ...

    @staticmethod
    @with_async_session
    async def retrieve(session: AsyncSession, obj_id: uuid.UUID) -> FeatureInternal | None: ...

    @staticmethod
    @with_async_session
    async def update(session: AsyncSession, obj: FeatureUpdateInternal) -> FeatureInternal: ...

    @staticmethod
    @with_async_session
    async def delete(session: AsyncSession, obj_id: uuid.UUID) -> None: ...

    @staticmethod
    @with_async_session
    async def list(session: AsyncSession, filter: FeatureFilter) -> list[FeatureInternal]: ...
```

### MessageStore specifics

Add helper to find current tail of a chat:

```python
@staticmethod
@with_async_session
async def get_last_message_id(session: AsyncSession, chat_id: uuid.UUID) -> uuid.UUID | None:
    stmt = (
        select(MessageDO.id)
        .where(MessageDO.chat_id == chat_id)
        .where(MessageDO.prev_message_id.is_(None))
    )
    # or simpler: select latest by created_at
    ...
```

For the simple pass, use `created_at DESC` / `id` ordering to find the most recent message.

### Join-table stores

Each memory entity store gets a link method:

```python
@staticmethod
@with_async_session
async def link_message(session: AsyncSession, event_id: uuid.UUID, message_id: uuid.UUID) -> None:
    session.add(EventMessageLinkDO(event_id=event_id, message_id=message_id))
    await session.flush()
```

Duplicate links raise `IntegrityError` from the composite PK and are translated to `*AlreadyExistProblem`.

---

## 9. Services

Services receive internal schemas and return internal schemas. They perform ownership checks via `context.user_id` and may call multiple stores.

### ChatGroupService

```python
class ChatGroupService:
    def __init__(self) -> None:
        self.store = ChatGroupStore

    async def create(self, chatgroup: ChatGroupCreateInternal) -> ChatGroupInternal:
        return await self.store.create(chatgroup)

    async def retrieve(self, chatgroup_id: uuid.UUID, user_id: uuid.UUID) -> ChatGroupInternal:
        chatgroup = await self.store.retrieve(chatgroup_id)
        if chatgroup is None or chatgroup.user_id != user_id:
            raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup_id} not found")
        return chatgroup

    async def update(self, chatgroup: ChatGroupUpdateInternal) -> ChatGroupInternal:
        existing = await self.retrieve(chatgroup.id, chatgroup.context.user_id)
        return await self.store.update(chatgroup)

    async def delete(self, chatgroup_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.retrieve(chatgroup_id, user_id)
        await self.store.delete(chatgroup_id)

    async def list(self, user_id: uuid.UUID, filter: ChatGroupFilter) -> CursorPage[ChatGroupInternal]:
        items = await self.store.list(user_id, filter)
        # build cursor envelope
        return CursorPage(items=items, next_cursor=None, has_more=False)
```

### ChatService

- `create` accepts `ChatCreateInternal`.
- If `chatgroup_id` is missing, call `ChatGroupStore.create` directly with a new ChatGroup (same name/description, current user's id).
- Verify the resulting ChatGroup is owned by `context.user_id`.
- Create the Chat.

```python
class ChatService:
    def __init__(self) -> None:
        self.store = ChatStore
        self.chatgroup_store = ChatGroupStore

    async def create(self, chat: ChatCreateInternal) -> ChatInternal:
        user_id = chat.context.user_id
        chatgroup_id = chat.chatgroup_id

        if chatgroup_id is None:
            chatgroup = await self.chatgroup_store.create(
                ChatGroupCreateInternal(
                    name=chat.name,
                    description=chat.description,
                    user_id=user_id,
                    context=ChatGroupContext(user_id=user_id),
                )
            )
            chatgroup_id = chatgroup.id
        else:
            chatgroup = await self.chatgroup_store.retrieve(chatgroup_id)
            if chatgroup is None or chatgroup.user_id != user_id:
                raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup_id} not found")

        return await self.store.create(
            ChatCreateInternal.model_validate(
                {**chat.model_dump(exclude_unset=True), "chatgroup_id": chatgroup_id}
            )
        )
```

### MessageService

On `create`:

1. Verify user owns the chat (walk up to ChatGroup).
2. Find last message id in the chat.
3. Build `MessageCreateInternal` with `prev_message_id=last_id`, server-generated `id`, default `processing_state` all `waiting`.
4. Save.

On `update`:

- `prev_message_id` is never changed; only `full_text` and `role` can be updated.
- Reset `processing_state` embedding/salience to `waiting` (event/topic/fact only if outside protected window; for simple pass reset all to waiting).

### EventService / TopicService / FactService

Standard CRUD with ownership checks plus `link_message` helper.

---

## 10. APIs

### 10.1 User

`POST /api/v1/users` is authenticated.

```python
@router.post("", response_model=UserRead)
async def create_user(
    user: UserCreate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> UserInternal:
    hashed_password = hash_password(user.password)
    user_internal = UserCreateInternal.model_validate(
        {"hashed_password": hashed_password, **user.model_dump(exclude={"password"}, exclude_unset=True)}
    )
    return await UserService().create(user_internal)
```

### 10.2 ChatGroup

```python
router = APIRouter(prefix="/api/v1/chatgroups", tags=["chatgroup"])

@router.post("", response_model=ChatGroupRead)
@router.get("/{chatgroup_id}", response_model=ChatGroupRead)
@router.patch("/{chatgroup_id}", response_model=ChatGroupRead)
@router.delete("/{chatgroup_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.get("", response_model=CursorPage[ChatGroupRead])
```

### 10.3 Chat

```python
router = APIRouter(prefix="/api/v1/chats", tags=["chat"])

@router.post("", response_model=ChatRead)
@router.get("/{chat_id}", response_model=ChatRead)
@router.patch("/{chat_id}", response_model=ChatRead)
@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)

@router.get("/chatgroups/{chatgroup_id}/chats", response_model=CursorPage[ChatRead])
```

### 10.4 Message

```python
router = APIRouter(prefix="/api/v1/chats/{chat_id}/messages", tags=["message"])

@router.post("", response_model=MessageRead)
async def create_message(
    chat_id: uuid.UUID,
    message: MessageCreate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> MessageInternal:
    message_internal = MessageCreateInternal.model_validate(
        {**message.model_dump(exclude_unset=True), "id": uuid.uuid4(), "chat_id": chat_id}
    )
    message_internal.context = MessageContext(user_id=current_user.id)
    return await MessageService().create(message_internal)

@router.patch("/{message_id}", response_model=MessageRead)
@router.get("/{message_id}", response_model=MessageRead)
@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.get("", response_model=CursorPage[MessageRead])
```

### 10.5 Event / Topic / Fact

Similar routers. Link endpoints:

```python
@router.post("/{event_id}/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
```

---

## 11. Migrations

Create `migrations/versions/0002_create_core_tables.py`:

- Enable `vector` extension.
- Create tables: `chatgroup`, `chat`, `message`, `event`, `topic`, `fact`.
- Create join tables: `event_message_links`, `topic_message_links`, `fact_message_links`.
- Add foreign keys and indexes.

Keep the migration async-online compatible like the existing `0001` revision.

---

## 12. Tests

### Shared helpers

Create `tests/backend/config.py` with a base polyfactory helper (equivalent of Accurial's `CustomSQLAlchemyModelFactory`):

```python
class BaseFactory(ModelFactory[T]):
    __is_base_factory__ = True

    @classmethod
    def to_api_response_data(cls, orm_model: Base | list[Base]):
        ...
```

### Per-module factories

Create `tests/backend/<feature>/factories.py` using polyfactory:

- `ChatGroupFactory`
- `ChatFactory`
- `MessageFactory`
- `EventFactory`
- `TopicFactory`
- `FactFactory`

### Test files

- `tests/backend/user/test_apis.py` — add `test_create_user_authenticated`.
- `tests/backend/chatgroup/test_apis.py`
- `tests/backend/chat/test_apis.py`
- `tests/backend/message/test_apis.py`
- `tests/backend/event/test_apis.py`
- `tests/backend/topic/test_apis.py`
- `tests/backend/fact/test_apis.py`

### Minimum test coverage

- Create, retrieve, update, delete happy path
- 404 on missing resource
- 403 when resource belongs to another user
- List returns cursor envelope
- Chat auto-creates ChatGroup when `chatgroup_id` is null
- Message POST auto-links `prev_message_id` to last message
- Message PATCH does not change `prev_message_id`
- `use_salience` defaults to `true` in ChatGroup config
- Duplicate join link returns 409

---

## 13. Files to Create/Modify

### Create

```
sources/backend/chatgroup/
  constants.py
  models.py
  schemas.py
  stores.py
  services.py
  apis.py

sources/backend/chat/
  models.py
  schemas.py
  stores.py
  services.py
  apis.py

sources/backend/message/
  constants.py
  models.py
  schemas.py
  stores.py
  services.py
  apis.py

sources/backend/event/
  constants.py
  models.py
  schemas.py
  stores.py
  services.py
  apis.py

sources/backend/topic/
  constants.py
  models.py
  schemas.py
  stores.py
  services.py
  apis.py

sources/backend/fact/
  constants.py
  models.py
  schemas.py
  stores.py
  services.py
  apis.py

migrations/versions/0002_create_core_tables.py

tests/backend/config.py
tests/backend/chatgroup/factories.py
tests/backend/chatgroup/test_apis.py
tests/backend/chat/factories.py
tests/backend/chat/test_apis.py
tests/backend/message/factories.py
tests/backend/message/test_apis.py
tests/backend/event/factories.py
tests/backend/event/test_apis.py
tests/backend/topic/factories.py
tests/backend/topic/test_apis.py
tests/backend/fact/factories.py
tests/backend/fact/test_apis.py
```

### Modify

```
pyproject.toml                         # add pgvector dependency
sources/backend/exceptions.py          # add entities and problems
sources/backend/main.py                # include new routers
sources/backend/user/apis.py           # add POST /users
sources/backend/user/schemas.py        # ensure UserCreateInternal works for public create
```

---

## 14. Rollout Plan

1. **Phase 1:** Dev environment (Python 3.14, uv, devbox, direnv, ParadeDB), Alembic migrations, user management, and JWT login.
2. **Phase 2:** Core entities, API, Postgres schema, basic message CRUD.
3. **Phase 3:** Real search infrastructure — filters, BM25/vector search APIs, ranking formula integration, and memory retrieval endpoints.
4. **Phase 4:** Embedding generation, memory ratio, and retrieval pipeline.
5. **Phase 5:** Batch embedding/salience jobs and batch consolidation (event, topic, fact).
6. **Phase 6:** Configurable prompts, ranking formula, and ChatGroup tuning.
7. **Phase 7:** Provider configuration file and packaging.

---

## 15. `use_salience` Summary

- Added as `use_salience: bool = True` inside `ChatGroupConfig`.
- Stored in the `config` JSONB column.
- This flag will later gate whether the batch worker computes and uses salience scores during retrieval.
- Default is `true`, meaning salience is active unless explicitly disabled.
