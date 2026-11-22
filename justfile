# Vistui development tasks

set dotenv-load

export VISTUI_DATABASE_URL := env("VISTUI_DATABASE_URL", "postgresql+psycopg_async://vistui:vistui@localhost:5433/vistui")
export VISTUI_TEST_DATABASE_URL := env("VISTUI_TEST_DATABASE_URL", "postgresql+psycopg_async://vistui:vistui@localhost:5434/vistui")

# Owner of the demo data: created on app boot and by the demo_local import scripts.
# Recipes that need different values override them inline.
export DEFAULT_EMAIL := env("DEFAULT_EMAIL", "demo@vistui.dev")
export DEFAULT_PASSWORD := env("DEFAULT_PASSWORD", "demopassword123")

# Start dev services (ParadeDB dev + test databases)
up:
    docker compose -f deployments/docker-compose.dev.yml up -d

# Stop and remove dev services
down:
    docker compose -f deployments/docker-compose.dev.yml down -v

# View dev service logs
logs:
    docker compose -f deployments/docker-compose.dev.yml logs -f

# Apply Alembic migrations to the dev database
migrate:
    PYTHONPATH=sources uv run alembic upgrade head

# Recreate the dev services from scratch (volumes wiped), apply the migrations, then
# import the demo_local data: default user, chatgroup, chat and 499 messages. The
# import scripts are idempotent except the messages one, which refuses to append to
# a chat that already has messages.
demo:
    docker compose -f deployments/docker-compose.dev.yml down -v
    docker compose -f deployments/docker-compose.dev.yml up -d --wait
    just migrate
    PYTHONPATH=sources uv run python demo_local/import_chatgroup.py
    PYTHONPATH=sources uv run python demo_local/import_chat.py
    PYTHONPATH=sources uv run python demo_local/import_messages.py

# Generate a new Alembic migration
migration name:
    PYTHONPATH=sources uv run alembic revision --autogenerate -m "{{name}}"

# Run the FastAPI development server
run:
    PYTHONPATH=sources uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Run the test suite (hermetic: LLM clients are mocked, integration tests are skipped)
test:
    VISTUI_DATABASE_URL=$VISTUI_TEST_DATABASE_URL \
    JWT_SECRET_KEY=OSEF \
    DEFAULT_EMAIL=test@example.com \
    DEFAULT_PASSWORD=devpassword123 \
    PYTHONPATH=sources:tests \
    uv run pytest -m "not integration"

# Run the batch integration tests against the real Ollama provider (slow, needs a
# reachable providers.yaml endpoint). Boots the app through its lifespan, which seeds
# the 30 demo messages on startup, then runs real consolidation worker rounds.
# The background scheduler is disabled (VISTUI_BATCH_INTERVAL_SECONDS=0) so only the
# admin-triggered rounds run: it would otherwise race with the test (its rounds drain
# the queue while the test's own slow round is still in flight).
# Override the number of rounds with VISTUI_INTEGRATION_MAX_ROUNDS (default 2).
test-integration:
    VISTUI_DATABASE_URL=$VISTUI_TEST_DATABASE_URL \
    VISTUI_INTEGRATION_TESTS=1 \
    VISTUI_SEED_DEMO=1 \
    VISTUI_BATCH_INTERVAL_SECONDS=0 \
    JWT_SECRET_KEY=OSEF \
    DEFAULT_EMAIL=test@example.com \
    DEFAULT_PASSWORD=devpassword123 \
    PYTHONPATH=sources:tests \
    uv run pytest -m integration -v

# Format code
fmt:
    uv run ruff format sources tests

# Lint code
lint:
    uv run ruff check sources tests

# Type check
typecheck:
    uv run mypy sources tests

# Run prek on all files
precommit:
    devbox run prek run --all-files

# Install git hooks
install_hooks:
    chmod +x deployments/hooks/*
    cp deployments/hooks/* .git/hooks/

# Enter the devbox shell
shell:
    devbox shell

# Install / sync dependencies
sync:
    uv sync

# Symlink dev configs into the project with stow
stow:
    cd devconfigs/stow && stow -t ../.. .
