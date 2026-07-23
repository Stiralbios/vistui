# Vistui Error Handling

Vistui uses a structured exception hierarchy for consistent API error responses.

## BaseProblem

All application exceptions inherit from `BaseProblem` in `sources/backend/exceptions.py`:

```python
class BaseProblem(Exception):
    kind: ProblemKind
    status: int
    entity: Entity

    def __init__(self, detail, *args):
        self.detail = detail
        ...

    @property
    def title(self):
        return f"{self.entity.title()} {self.kind.title().replace('_', ' ')}"
```

## Design Logic

Exceptions are organized along two axes:

1. **Entity** — what domain object is affected (`USER`, `CHATGROUP`, `CHAT`, `MESSAGE`, `EVENT`, `TOPIC`, `FACT`, `MEMORY`).
2. **ProblemKind** — what went wrong (`NOT_FOUND`, `ALREADY_EXIST`, `NOT_ALLOWED`, `CONFLICT`, `INVALID`).

This creates a matrix: `<Entity><ProblemKind>Problem`.

Examples:

- `MessageNotFoundProblem` — a message was requested but does not exist.
- `ChatGroupAlreadyExistProblem` — trying to create a duplicate ChatGroup.
- `ChatNotAllowedProblem` — an action on a Chat is forbidden.
- `MessageConflictProblem` — linked-list inconsistency.

## HTTP Response Format

Handled by `problem_exception_handler` in `main.py`:

```python
@app.exception_handler(BaseProblem)
async def problem_exception_handler(request: Request, exc: BaseProblem):
    return JSONResponse(
        status_code=exc.status,
        content={
            "type": exc.kind,
            "on": exc.entity,
            "title": exc.title,
            "detail": exc.detail,
            "status": exc.status
        },
    )
```

Example response (404):

```json
{
  "type": "NOT_FOUND",
  "on": "MESSAGE",
  "title": "Message Not Found",
  "detail": "Message abc-123 not found",
  "status": 404
}
```

## Foreign Key Error Handling

`handle_foreign_key_violation()` in `utils/error_handling.py` parses PostgreSQL integrity errors and maps them to domain exceptions based on the violated column name:

```python
def handle_foreign_key_violation(error: sqlalchemy.exc.IntegrityError) -> None:
    fk_exception_map = {
        "chat_id": ChatNotFoundProblem,
        "chatgroup_id": ChatGroupNotFoundProblem,
        "user_id": UserNotFoundProblem,
        "prev_message_id": MessageNotFoundProblem,
        "next_message_id": MessageNotFoundProblem,
    }
```

## Linked List Errors

Linked-list inconsistencies are raised as `MessageConflictProblem` with a 409 status:

- `prev_message_id` references a message in another Chat.
- `prev_message_id` and `next_message_id` create a cycle.
- `prev_message_id` points to a message that already has a different next pointer.

## Adding New Exceptions

When adding a new feature, create the standard variants in `exceptions.py`:

```python
class FeatureNotFoundProblem(BaseProblem):
    kind = ProblemKind.NOT_FOUND
    status = status.HTTP_404_NOT_FOUND
    entity = Entity.FEATURE

class FeatureAlreadyExistProblem(BaseProblem):
    kind = ProblemKind.ALREADY_EXIST
    status = status.HTTP_409_CONFLICT
    entity = Entity.FEATURE

class FeatureNotAllowedProblem(BaseProblem):
    kind = ProblemKind.NOT_ALLOWED
    status = status.HTTP_403_FORBIDDEN
    entity = Entity.FEATURE
```

## Files

- `sources/backend/exceptions.py` — exception hierarchy.
- `sources/backend/utils/error_handling.py` — FK violation and integrity error mapping.
- `sources/backend/main.py` — exception handler registration.
