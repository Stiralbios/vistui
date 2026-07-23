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


class InitSettings(BaseSettings):
    DEFAULT_EMAIL: str | None = None
    DEFAULT_PASSWORD: SecretStr | None = None
