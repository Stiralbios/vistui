import json
import os
from typing import Any

from backend.llm.config import (
    LLMDefaults,
    ProviderConfig,
    ProviderSpec,
    get_provider_config,
)
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from loguru import logger
from pydantic import SecretStr


class LLMProviderError(Exception):
    pass


class EmbeddingError(LLMProviderError):
    pass


class ChatError(LLMProviderError):
    pass


class Provider:
    """LangChain-based async provider wrapper used by the batch worker.

    Provider and model selection is driven by ``providers.yaml`` (see
    ``documentations/architecture/llm-provider.md``). The ``task`` argument to
    ``chat`` selects the per-task override defined in that file.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config or get_provider_config()
        self._embedding_model = self._build_embedding_model()

    def _api_key(self, spec: ProviderSpec) -> SecretStr:
        if spec.api_key_env and (key := os.environ.get(spec.api_key_env)):
            return SecretStr(key)
        return SecretStr("ollama")

    def _build_embedding_model(self) -> OpenAIEmbeddings:
        embedding_defaults = self.config.defaults.embedding
        provider = self.config.providers[embedding_defaults.provider]
        return OpenAIEmbeddings(
            base_url=provider.base_url,
            api_key=self._api_key(provider),
            model=embedding_defaults.model,
            dimensions=embedding_defaults.dimensions,
            check_embedding_ctx_length=False,
            timeout=provider.timeout_seconds,
        )

    def _resolve_llm_config(
        self,
        task: str,
        temperature: float | None,
        max_tokens: int | None,
    ) -> LLMDefaults:
        defaults = self.config.defaults.llm
        override = self.config.overrides.get(task)
        if override is None:
            resolved_temperature = temperature if temperature is not None else defaults.temperature
            resolved_max_tokens = max_tokens if max_tokens is not None else defaults.max_tokens
            return LLMDefaults(
                provider=defaults.provider,
                model=defaults.model,
                temperature=resolved_temperature,
                max_tokens=resolved_max_tokens,
            )

        return LLMDefaults(
            provider=override.provider or defaults.provider,
            model=override.model or defaults.model,
            temperature=override.temperature
            if temperature is None and override.temperature is not None
            else (temperature if temperature is not None else defaults.temperature),
            max_tokens=override.max_tokens
            if max_tokens is None and override.max_tokens is not None
            else (max_tokens if max_tokens is not None else defaults.max_tokens),
        )

    def _build_chat_model(self, llm_config: LLMDefaults) -> ChatOpenAI:
        provider = self.config.providers[llm_config.provider]
        return ChatOpenAI(
            base_url=provider.base_url,
            api_key=self._api_key(provider),
            model=llm_config.model,
            temperature=llm_config.temperature,
            max_completion_tokens=llm_config.max_tokens,
            timeout=provider.timeout_seconds,
            reasoning_effort="none",
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        try:
            vectors = await self._embedding_model.aembed_documents(texts)
        except Exception as exc:
            logger.exception("Embedding request failed")
            raise EmbeddingError(f"Embedding request failed: {exc}") from exc

        embeddings: list[list[float]] = []
        expected = self.config.defaults.embedding.dimensions
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != expected:
                raise EmbeddingError(
                    f"Unexpected embedding dimension: got {len(vector) if isinstance(vector, list) else type(vector)}, "
                    f"expected {expected}"
                )
            embeddings.append(vector)

        return embeddings

    async def chat(
        self,
        messages: list[dict[str, str]],
        task: str = "default",
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        llm_config = self._resolve_llm_config(task, temperature, max_tokens)
        model: Any = self._build_chat_model(llm_config)

        try:
            lc_messages: list[HumanMessage | AIMessage] = []
            for message in messages:
                content = message.get("content", "")
                if message.get("role") == "user":
                    lc_messages.append(HumanMessage(content=content))
                elif message.get("role") == "assistant":
                    lc_messages.append(AIMessage(content=content))
                else:
                    lc_messages.append(HumanMessage(content=content))

            if json_mode:
                model = model.bind(response_format={"type": "json_object"})

            response = await model.ainvoke(lc_messages)
        except Exception as exc:
            logger.exception("Chat completion request failed")
            raise ChatError(f"Chat completion request failed: {exc}") from exc

        content = response.content if isinstance(response.content, str) else str(response.content)
        if not content:
            content = getattr(response, "reasoning_content", None) or ""
        if not content:
            raise ChatError("Chat completion returned no content")
        return content

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        task: str = "default",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        content = await self.chat(
            messages,
            task=task,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error(f"Failed to parse LLM JSON response: {content}")
            raise ChatError(f"Invalid JSON from chat model: {exc}") from exc
