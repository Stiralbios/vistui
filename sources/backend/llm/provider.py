import json
import os
from typing import Any, Literal

import httpx
from backend.llm.config import (
    LLMDefaults,
    ProviderConfig,
    ProviderSpec,
    get_provider_config,
)
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class LLMProviderError(Exception):
    pass


class EmbeddingError(LLMProviderError):
    pass


class ChatError(LLMProviderError):
    pass


class DecisionError(LLMProviderError):
    pass


class ChoiceQuestion(BaseModel):
    """System One question: pick the best-fitting option among labeled criteria."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["choice"] = "choice"
    instructions: Any
    criteria: dict[str, str | None] = Field(min_length=2, max_length=255)


class NoulQuestion(BaseModel):
    """System One question: probability of true for a yes/no instruction.

    Optional per-outcome descriptions live in `criteria` under the literal keys
    `false` and `true`.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["noul"] = "noul"
    instructions: Any
    criteria: dict[Literal["false", "true"], str] | None = None


class ScoreQuestion(BaseModel):
    """System One question: score the state against ordered rubric levels.

    `criteria` lists 2-10 level descriptions from the lowest (index 0) to the
    highest; the answer's `score` is a probability-weighted average of the
    zero-based indices, i.e. 0 .. len(criteria) - 1.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["score"] = "score"
    instructions: Any
    criteria: list[str] = Field(min_length=2, max_length=10)


SystemOneQuestion = ChoiceQuestion | NoulQuestion | ScoreQuestion


class DecisionAnswer(BaseModel):
    """One System One answer; the fields present depend on the question type
    (choice -> `choice`/`probabilities`, noul -> `noul`, score -> `score`/`legend`)."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["choice", "noul", "score"]
    choice: str | None = None
    noul: float | None = None
    score: float | None = None
    legend: dict[str, str] | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = None


class Provider:
    """LangChain-based async provider wrapper used by the batch worker.

    Provider and model selection is driven by ``settings.yaml`` (see
    ``documentations/architecture/llm-provider.md``). The ``task`` argument to
    ``chat`` selects the per-task override defined in that file. Decision models
    (``decide``) are System One models called through ollama's ``/v1/systemone``
    endpoint and configured under ``defaults.decision``.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config or get_provider_config()
        self._embedding_model = self._build_embedding_model()

    def _api_key(self, spec: ProviderSpec) -> SecretStr:
        if spec.api_key_env and (key := os.environ.get(spec.api_key_env)):
            return SecretStr(key)
        return SecretStr("ollama")

    def _build_embedding_model(self) -> OpenAIEmbeddings:
        embedding_defaults = self.config.defaults_models.embedding
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
        defaults = self.config.defaults_models.llm
        override = self.config.overrides_models.get(task)
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
        expected = self.config.defaults_models.embedding.dimensions
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

    async def decide(
        self,
        state: Any,
        questions: dict[str, SystemOneQuestion],
        task: str | None = None,
        keep_alive: str | float | None = None,
    ) -> dict[str, DecisionAnswer]:
        """Ask the configured decision model typed questions about a shared state.

        Decision models (e.g. ``d3:4b``) are System One models: they answer with
        probabilities and rubric scores on ollama's ``POST /v1/systemone`` endpoint
        and have no chat-completions API. ``state`` is any JSON-serializable text,
        object or array shared by all ``questions`` (keyed by name, 1-64 of them);
        answers come back keyed by the same names. ``task`` optionally selects a
        per-task override (provider/model) the same way chat tasks do, falling back
        to ``defaults.decision``. ``keep_alive`` optionally keeps the model loaded
        between batch requests (duration string or seconds).
        """
        if not questions:
            raise DecisionError("decision requests need at least one question")

        spec, model = self._resolve_decision_config(task)
        payload: dict[str, Any] = {
            "model": model,
            "state": state,
            "questions": {name: question.model_dump(exclude_none=True) for name, question in questions.items()},
        }
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive

        response = await self._systemone_request(spec, payload)
        raw_answers = response.get("answers")
        if not isinstance(raw_answers, dict):
            raise DecisionError("System One response has no answers object")
        missing = set(questions) - set(raw_answers)
        if missing:
            raise DecisionError(f"System One response is missing answers for: {sorted(missing)}")

        answers: dict[str, DecisionAnswer] = {}
        for name, raw in raw_answers.items():
            try:
                answers[name] = DecisionAnswer.model_validate(raw)
            except ValidationError as exc:
                raise DecisionError(f"Malformed System One answer for question {name!r}: {exc}") from exc

        usage = response.get("usage") or {}
        logger.debug(
            f"System One call for {len(questions)} question(s) on model {model}: "
            f"input_tokens={usage.get('input_tokens')} output_tokens={usage.get('output_tokens')}"
        )
        return answers

    def _resolve_decision_config(self, task: str | None) -> tuple[ProviderSpec, str]:
        """Resolve the provider spec and model for a decision call: the per-task
        override when one exists (same overrides map as chat tasks), else
        ``defaults.decision``."""
        decision = self.config.defaults_models.decision
        provider_name, model = decision.provider, decision.model
        if task is not None and (override := self.config.overrides_models.get(task)) is not None:
            provider_name = override.provider or provider_name
            model = override.model or model
        return self.config.providers[provider_name], model

    async def _systemone_request(self, spec: ProviderSpec, payload: dict[str, Any]) -> dict[str, Any]:
        """POST one System One request and return the parsed JSON response."""
        try:
            async with httpx.AsyncClient(timeout=spec.timeout_seconds) as client:
                response = await client.post(
                    f"{spec.base_url}/systemone",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key(spec).get_secret_value()}"},
                )
        except httpx.HTTPError as exc:
            logger.exception("System One request failed")
            raise DecisionError(f"System One request failed: {exc}") from exc
        if response.status_code != 200:
            raise DecisionError(f"System One call failed with HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        if not isinstance(data, dict):
            raise DecisionError("System One response was not a JSON object")
        return data
