# Vistui Schema Strategy

How request/response schemas are organized in Vistui.

## Schema Naming Convention

Every feature defines the following schema types:

| Type | Purpose | Used By |
|------|---------|---------|
| `FeatureBase` | Shared fields | Inherited |
| `FeatureRead` | Full object returned to clients | API responses |
| `FeatureInternal` | Superset used internally (API -> Service -> Store) | Internal flow |
| `FeatureCreate` | Request body for creation | Client POST/PUT |
| `FeatureCreateInternal` | Internal creation with injected fields | API -> Service |
| `FeatureUpdate` | Request body for updates (all Optional) | Client PATCH |
| `FeatureUpdateInternal` | Internal update with context | API -> Service |
| `FeatureFilter` | Query parameters for listing | GET /?params |

## External vs Internal

### External Schemas

- Sent/received between clients and backend.
- Do not contain internal-only fields (e.g., `chat_id` injected by the API).
- Validated by FastAPI's request/response handling.

Example — `MessageCreate` (external):

```python
class MessageCreate(MessageBase):
    model_config = ConfigDict(extra="forbid")
    prev_message_id: uuid.UUID | None = None
    next_message_id: uuid.UUID | None = None
    full_text: str
    role: MessageRole
```

### Internal Schemas

- Used in service and store layers.
- May contain fields injected by the API layer.
- Include `context` for business logic.

Example — `MessageCreateInternal` (internal):

```python
class MessageCreateInternal(MessageCreate):
    id: uuid.UUID  # caller-provided, injected by API
    chat_id: uuid.UUID  # injected by API
    context: MessageSaveContext
```

## Context Pattern

The API layer injects a `context` object into internal schemas to pass operation metadata to services:

```python
class MessageSaveContext(BaseModel):
    retrieve: bool
    user_id: uuid.UUID

class MessageCreateInternal(MessageCreate):
    context: MessageSaveContext
```

`context` is never used by stores. It is excluded during `model_dump()`:

```python
for field, value in message.model_dump(exclude_unset=True, exclude={"context"}).items():
    setattr(orm_object, field, value)
```

## Common Patterns

### Preventing Explicit None in Updates

Update schemas use `field_validator` to reject explicit `None` values:

```python
@field_validator("name", "description", mode="before")
def prevent_explicit_none(cls, value: Any) -> Any:
    if value is None:
        raise ValueError("Explicit None is not allowed for this field")
    return value
```

This ensures PATCH only updates specified fields while preventing accidental clearing.

### Factory Pattern for Conversion

The API layer combines external schema with injected fields using `model_validate`:

```python
message_internal = MessageCreateInternal.model_validate(
    {**message.model_dump(exclude_unset=True), "id": message_id, "chat_id": chat_id}
)
message_internal.context = MessageSaveContext(retrieve=retrieve, user_id=user.id)
```

### Retrieval Response Schemas

Retrieval uses dedicated external schemas:

```python
class MemoryItem(BaseModel):
    type: MemoryType
    id: uuid.UUID
    full_text: str
    relevance: float
    estimated_tokens: int
    metadata: MemoryItemMetadata

class MemoryResponse(BaseModel):
    token_budget: int
    memory_ratio: MemoryRatio
    used_tokens: int
    items: list[MemoryItem]
```

### Filter Schemas

List endpoints use explicit query parameter schemas:

```python
class MessageFilter(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: uuid.UUID | None = None
    role: MessageRole | None = None
```

Used in list endpoints:

```python
@router.get("/{chat_id}/messages", response_model=list[MessageRead])
async def list_messages(
    chat_id: uuid.UUID,
    message_filter: MessageFilter = Depends(),
) -> list[MessageRead]: ...
```
