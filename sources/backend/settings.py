from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class AppSettings(BaseSettings):
    APP_ENVIRONMENT: Literal["DEV", "TEST", "PROD"] = "DEV"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    JWT_ALGORITHM: str = "HS256"
    JWT_SECRET_KEY: SecretStr = SecretStr("OSEF")
    JWT_ACCESS_TOKEN_EXPIRATION_MINUTES: int = 60
    VISTUI_DATABASE_URL: SecretStr = SecretStr("postgresql+psycopg_async://vistui:vistui@localhost:5433/vistui")
    ALLOWED_CORS_ORIGINS: list[str] = ["*"]
    VISTUI_SEED_DEMO: bool = False
    # Default protected window — the x most recent messages of a chat that are never
    # added to facts/events/topics until newer messages push them out of the window.
    # ChatGroups can override it per group via their config's protected_message_count.
    VISTUI_PROTECTED_MESSAGE_COUNT: int = 10
    VISTUI_BATCH_INTERVAL_SECONDS: int = 60
    # Batch worker: per-round work limits of the batch stages (the consolidation
    # pipelines event/topic/fact are single-message by design). See
    # documentations/architecture/batch-consolidation.md.
    VISTUI_BATCH_EMBEDDING_LIMIT: int = 32
    VISTUI_BATCH_MEMORY_EMBEDDING_LIMIT: int = 32
    VISTUI_BATCH_SALIENCE_LIMIT: int = 5
    VISTUI_BATCH_MEMORY_SALIENCE_LIMIT: int = 32
    VISTUI_BATCH_EVENT_BLOCK_SIZE: int = 10


class InitSettings(BaseSettings):
    DEFAULT_EMAIL: str | None = None
    DEFAULT_PASSWORD: SecretStr | None = None
