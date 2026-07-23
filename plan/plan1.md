# Vistui Milestone 1: Dev Environment + Login

## Goal

Get a minimal but production-grade Vistui backend running locally with:
- A reproducible dev environment (devbox + direnv + uv)
- ParadeDB PostgreSQL for development and testing via Docker Compose
- Alembic migrations
- JWT-based OAuth2 password login
- A single seeded default user from environment variables
- A `GET /api/v1/auth/me` endpoint protected by the same JWT
- A `GET /health` endpoint that checks the database

No public user registration, no ChatGroup/Message/retrieval/batch features yet.

## Decisions

| Topic | Decision |
|-------|----------|
| Python | 3.14 |
| Package/venv manager | `uv` |
| Dev shell | `devbox` + `direnv` |
| Database | `paradedb/paradedb:pg18` via Docker Compose (dev on 5433, test on 5434) |
| Public registration | **No** — the only initial user is seeded from env |
| Initial user | `DEFAULT_EMAIL` / `DEFAULT_PASSWORD` env vars |
| Login endpoint | `POST /api/v1/auth/jwt/login` |
| Current user endpoint | `GET /api/v1/auth/me` (requires token) |
| API prefix | `/api/v1` |
| Migrations | Alembic; applied manually via `just migrate` during development |
| Logging | Text, colored, via loguru |
| Git hooks | `prek` (drop-in Rust replacement for pre-commit) |

## Project layout

```
/home/astyan/Source/Vistui/
├── devbox.json
├── devbox.lock
├── .envrc
├── .env
├── justfile
├── pyproject.toml
├── .gitignore
├── plan/
│   └── plan1.md          <- this file
├── deployments/
│   └── docker-compose.dev.yml
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_create_user_table.py
├── sources/
│   └── backend/
│       ├── main.py
│       ├── settings.py
│       ├── database.py
│       ├── exceptions.py
│       ├── logconfig.py
│       ├── seeders.py
│       ├── health/apis.py
│       ├── auth/
│       │   ├── apis.py
│       │   ├── services.py
│       │   ├── dependencies.py
│       │   └── schemas.py
│       ├── user/
│       │   ├── models.py
│       │   ├── schemas.py
│       │   ├── stores.py
│       │   ├── services.py
│       │   └── apis.py
│       └── utils/security.py
└── tests/
    └── backend/
        ├── conftest.py
        └── auth/test_login.py
```

## Implementation steps

### 1. Dev environment

Create:
- `devbox.json` with Python 3.14, uv, just, prek, PostgreSQL CLI tools, gcc
- `.envrc` for direnv + devbox integration
- `.env` for `PYTHONPATH=sources`
- `pyproject.toml` with `uv` metadata, runtime and dev dependencies
- `justfile` for common tasks
- `deployments/docker-compose.dev.yml` with dev and test ParadeDB containers
- `.pre-commit-config.yaml`, `.flake8`, `.bandit`, `mypy.ini`
- `deployments/hooks/pre-commit` and `pre-push` using `prek`
- Update `.gitignore`

### 2. Migrations

- Initialize Alembic (`alembic init migrations`)
- Configure `migrations/env.py` for async psycopg and SQLAlchemy `Base`
- Initial revision creating the `user` table

### 3. Backend core

Implement in `sources/backend/`:
- `settings.py` — `AppSettings` and `InitSettings` via pydantic-settings
- `database.py` — async engine, session context manager, `with_async_session` decorator
- `exceptions.py` — `BaseProblem` + user-specific problems
- `logconfig.py` — loguru text logging
- `utils/security.py` — Argon2id hashing/verification
- `seeders.py` — create default user from env at startup

### 4. User feature

Implement in `sources/backend/user/`:
- `models.py` — `UserDO` SQLAlchemy model
- `schemas.py` — `UserBase`, `UserRead`, `UserInternal`, `UserCreate`, `UserCreateInternal`
- `stores.py` — `create`, `retrieve`, `retrieve_by_email`
- `services.py` — `create`, `authenticate`
- `apis.py` — no public create endpoint in this milestone

### 5. Auth feature

Implement in `sources/backend/auth/`:
- `services.py` — JWT creation with PyJWT
- `dependencies.py` — `get_current_user`, `get_current_active_user`
- `schemas.py` — `Token`
- `apis.py` — `POST /api/v1/auth/jwt/login`, `GET /api/v1/auth/me`

### 6. App entry point

Implement:
- `sources/backend/main.py` — lifespan, routers, CORS, global exception handler
- `sources/backend/health/apis.py` — `GET /health` checking DB connectivity

### 7. Tests

Implement:
- `tests/backend/conftest.py` — async test client + test DB setup
- `tests/backend/auth/test_login.py` — login success, login failure, `/me` with/without token

### 8. Architecture doc updates

Update:
- `documentations/architecture/infrastructure.md` — Python 3.14
- `documentations/architecture/system-overview.md` — Phase 1 details
- `documentations/architecture/auth.md` — no public registration in v1, default user seeding

## Verification

```bash
# start the database
just up

# apply migrations
just migrate

# run prek hooks
just precommit

# start the server
just run
```

Then:

```bash
# login as the seeded default user
curl -X POST http://localhost:8000/api/v1/auth/jwt/login \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=test@example.com&password=devpassword123'

# current user
curl http://localhost:8000/api/v1/auth/me \
  -H 'Authorization: Bearer <token>'

# health check
curl http://localhost:8000/health
```

## Out of scope

- Public user registration
- ChatGroup, Chat, Message, Event, Topic, Fact
- Vector search / BM25 / retrieval
- Batch worker / APScheduler
- Provider configuration / LLM calls
- Frontend

## Status

**Completed** ✅

All implementation steps are done, tests pass, prek hooks run successfully, and the changes are pushed to `origin main`.

## Changelog

- 2026-07-23: Created plan1.md for the login-only milestone.
- 2026-07-23: Switched git hooks manager from pre-commit to prek.
- 2026-07-23: Marked milestone as completed.
