# Vistui development tasks

set dotenv-load

export VISTUI_DATABASE_URL := env("VISTUI_DATABASE_URL", "postgresql+psycopg_async://vistui:vistui@localhost:5433/vistui")
export VISTUI_TEST_DATABASE_URL := env("VISTUI_TEST_DATABASE_URL", "postgresql+psycopg_async://vistui:vistui@localhost:5434/vistui")

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

# Generate a new Alembic migration
migration name:
    PYTHONPATH=sources uv run alembic revision --autogenerate -m "{{name}}"

# Run the FastAPI development server
run:
    PYTHONPATH=sources uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Run the test suite
test:
    VISTUI_DATABASE_URL=$VISTUI_TEST_DATABASE_URL \
    JWT_SECRET_KEY=OSEF \
    DEFAULT_EMAIL=test@example.com \
    DEFAULT_PASSWORD=devpassword123 \
    PYTHONPATH=sources \
    uv run pytest

# Format code
fmt:
    uv run ruff format sources tests

# Lint code
lint:
    uv run ruff check sources tests

# Type check
typecheck:
    uv run mypy sources tests

# Run pre-commit on all files
precommit:
    devbox run pre-commit run --all-files

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
