# Vistui Testing

Backend testing with pytest.

## Test Configuration

- **Framework**: pytest with async support (`pytest-asyncio`).
- **Mode**: `auto` (async fixtures/tests detected automatically).
- **Coverage**: pytest-cov with a minimum threshold configured in `pyproject.toml`.
- **Factories**: polyfactory.
- **Mocking**: pytest-mock.
- **Time freezing**: freezegun.

## Test Database

Tests use a separate PostgreSQL instance, typically at `localhost:5434`.

### Database Setup (`conftest.py`)

- Uses `psycopg_async` engine.
- **TRUNCATE** all tables between tests (faster than drop/create).
- Falls back to drop/create if new tables/columns are added.
- Patches `backend.database._engine_instance` and `_async_sessionmaker` for isolation.

### Fixtures

| Fixture | Scope | Description |
|---------|-------|-------------|
| `mock_engine_and_session` | function | Isolated DB engine, truncates tables. |
| `client_fixture` | function | HTTPX async client with auth override. |
| `anyio_backend` | session | Fixes double test issue with anyio. |

### Auth Override

Tests automatically create an authenticated admin user:

```
admin@test.lan / password (hashed)
```

The `client_fixture` overrides `get_current_active_user` dependency for tests. Can be disabled with `@pytest.mark.parametrize("client_fixture", [False], indirect=True)`.

## Test File Structure

```
tests/backend/
├── conftest.py
├── config.py
├── debug/
│   └── test_apis.py
├── user/
│   ├── factories.py
│   └── test_apis.py
├── chatgroup/
│   ├── factories.py
│   └── test_apis.py
├── chat/
│   ├── factories.py
│   └── test_apis.py
├── message/
│   ├── factories.py
│   ├── test_apis.py
│   └── test_linked_list.py
├── event/
│   ├── factories.py
│   └── test_apis.py
├── topic/
│   ├── factories.py
│   └── test_apis.py
├── fact/
│   ├── factories.py
│   └── test_apis.py
└── batch/
    └── test_worker.py
```

## Running Tests

```bash
# All tests
just test

# With coverage
uv run pytest --cov

# Specific file
uv run pytest tests/backend/message/test_apis.py

# Specific test
uv run pytest tests/backend/message/test_apis.py::test_save_message -v
```

## Linked List Tests

The `message` feature has dedicated `test_linked_list.py` covering:

- Inserting a message at the head, middle, and tail.
- Editing a message without changing neighbors.
- Deleting a message and repairing the list.
- Rejecting cycles and cross-Chat references.

## Factory Pattern

Factories use polyfactory for test data creation:

```python
from polyfactory.factories.pydantic_factory import ModelFactory
from backend.message.schemas import MessageCreate

class MessageFactory(ModelFactory[MessageCreate]):
    __model__ = MessageCreate
    full_text = "Test message"
    role = "user"
```

## Files

- `tests/backend/conftest.py` — global fixtures.
- `tests/backend/config.py` — test config.
- `tests/backend/<feature>/factories.py` — feature factories.
- `tests/backend/<feature>/test_apis.py` — API tests.
