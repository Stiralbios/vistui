from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings


class LLMSettings(BaseSettings):
    """Legacy environment-based settings used as a fallback when providers.yaml is missing."""

    VISTUI_PROVIDERS_FILE: str = "./providers.yaml"
    VISTUI_LLM_BASE_URL: str = "http://192.168.9.137:11434/v1"
    VISTUI_LLM_API_KEY: SecretStr | None = None
    VISTUI_EMBEDDING_MODEL: str = "qwen3-embedding:8b"
    VISTUI_CHAT_MODEL: str = "gemma4:e4b"
    VISTUI_EMBEDDING_DIMENSIONS: int = 768
    VISTUI_LLM_TIMEOUT_SECONDS: int = 120


class ProviderSpec(BaseModel):
    """Connection details for a single OpenAI-compatible provider."""

    base_url: str
    api_key_env: str | None = None
    timeout_seconds: int = 120


class EmbeddingDefaults(BaseModel):
    """Default provider/model/dimensions for embedding calls."""

    provider: str
    model: str
    dimensions: int


class LLMDefaults(BaseModel):
    """Default provider/model/temperature for chat calls."""

    provider: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 2048


class DefaultsConfig(BaseModel):
    embedding: EmbeddingDefaults
    llm: LLMDefaults


class TaskConfig(BaseModel):
    """Per-task override for a chat call."""

    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class ProviderConfig(BaseModel):
    """Loaded from providers.yaml."""

    providers: dict[str, ProviderSpec]
    defaults: DefaultsConfig
    overrides: dict[str, TaskConfig] = Field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> Self:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return cls(**data)

    @classmethod
    def from_env(cls, settings: LLMSettings | None = None) -> Self:
        """Build a single-provider config from legacy environment variables."""
        settings = settings or LLMSettings()
        api_key_env: str | None = None
        if settings.VISTUI_LLM_API_KEY:
            api_key_env = "VISTUI_LLM_API_KEY"
        return cls(
            providers={
                "default": ProviderSpec(
                    base_url=settings.VISTUI_LLM_BASE_URL,
                    api_key_env=api_key_env,
                    timeout_seconds=settings.VISTUI_LLM_TIMEOUT_SECONDS,
                )
            },
            defaults=DefaultsConfig(
                embedding=EmbeddingDefaults(
                    provider="default",
                    model=settings.VISTUI_EMBEDDING_MODEL,
                    dimensions=settings.VISTUI_EMBEDDING_DIMENSIONS,
                ),
                llm=LLMDefaults(
                    provider="default",
                    model=settings.VISTUI_CHAT_MODEL,
                    temperature=0.2,
                    max_tokens=2048,
                ),
            ),
        )


_config: ProviderConfig | None = None


def get_provider_config() -> ProviderConfig:
    """Return the loaded provider config, falling back to env-based defaults."""
    global _config
    if _config is None:
        settings = LLMSettings()
        path = Path(settings.VISTUI_PROVIDERS_FILE).expanduser()
        if path.exists():
            _config = ProviderConfig.from_file(path)
        else:
            _config = ProviderConfig.from_env(settings)
    return _config


def reload_provider_config() -> ProviderConfig:
    """Force a reload of the provider config from disk or env."""
    global _config
    _config = None
    return get_provider_config()
