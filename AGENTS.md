# Vistui

Self-hosted REST API that stores and retrieves memory for long-running chatbot
conversations. Raw messages are asynchronously consolidated by batch jobs into
structured memory (events, topics, facts), which is retrieved as context for new
messages. Frontend-agnostic: consumed by chatbot frontends / MCP servers.

## Technologies

Python 3.14 · FastAPI + Pydantic · SQLAlchemy (async PostgreSQL with pgvector +
pg_search) · Alembic migrations · APScheduler worker · LangChain / OpenAI-compatible
LLM providers (configured in `providers.yaml`) · uv, devbox, just.

## Where things are

| Path | Content |
|------|---------|
| `sources/backend/` | Backend source code (API, models, stores, batch jobs, LLM adapter) |
| `tests/backend/` | Tests, mirroring `sources/backend/` structure |
| `documentations/architecture/` | Architecture docs — start with `system-overview.md` |
| `migrations/`, `alembic.ini` | Database migrations |
| `plan/` | Design / issue planning notes |

## Commands

- `just up` / `just down` — start/stop dev services (ParadeDB dev + test DBs via Docker)
- `just test` — hermetic test suite (LLM clients mocked, integration tests skipped)
- `just test-integration` — slow, real-LLM batch tests (opt-in)
- `just run` — FastAPI dev server on port 8000
- `just migrate` / `just migration <name>` — apply / generate Alembic migrations
- `just fmt` / `just lint` / `just typecheck` — ruff format, ruff check, mypy
- `just sync` — install/sync dependencies with uv

## Conventions

- API base URL: `/api/v1` · health check: `GET /health`
- TDD: write failing tests first; run relevant tests after every change
- Line length 120, double quotes, `ruff check` clean, mypy strict-ish
- Keep comments short. 
- Do not reference plan, issues in the comments. You can reference documentations
- Do not reference the previous state in comments. Only what they do right here and now