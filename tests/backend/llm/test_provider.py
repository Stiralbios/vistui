"""Unit tests for the decision-model (System One) call path.

The decision models are mocked at the HTTP boundary (``httpx.AsyncClient`` in
``backend.llm.provider``), mirroring how the LangChain clients are faked elsewhere.
"""

from unittest.mock import patch

import httpx
import pytest
from backend.llm.config import LLMSettings, ProviderConfig
from backend.llm.provider import (
    ChoiceQuestion,
    DecisionAnswer,
    DecisionError,
    NoulQuestion,
    Provider,
    ScoreQuestion,
)
from pydantic import ValidationError


def _test_config(base_url: str = "http://ollama.test/v1") -> ProviderConfig:
    """A single-provider config pointing at a fake host; no network is touched."""
    return ProviderConfig.from_env(
        LLMSettings(
            VISTUI_LLM_BASE_URL=base_url,
            VISTUI_EMBEDDING_MODEL="embeddinggemma-2:740m",
            VISTUI_CHAT_MODEL="gemma4:e4b",
            VISTUI_DECISION_MODEL="d3:4b",
        )
    )


def _questions() -> dict:
    """The three question types, mirroring the live d3 validation request."""
    return {
        "salience": ScoreQuestion(
            instructions="How salient is this memory for long-term recall about the user?",
            criteria=[
                "trivial smalltalk, safe to forget",
                "mildly interesting context",
                "notable preference or fact about the user",
                "important milestone or strong preference",
                "critical life event",
            ],
        ),
        "keep": NoulQuestion(
            instructions="Is this message worth remembering in the long term?",
            criteria={"false": "Smalltalk or noise", "true": "Contains durable information"},
        ),
        "mood": ChoiceQuestion(
            instructions="What is the emotional tone of this message?",
            criteria={"positive": "Happy or excited", "negative": "Sad or angry", "neutral": "No clear emotion"},
        ),
    }


def _fake_systemone_client(captured: list, response: dict, status_code: int = 200, exc: Exception | None = None):
    """Build an httpx.AsyncClient replacement that records the request and replays a response."""

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured.append({"url": url, "json": json, "headers": headers})
            if exc is not None:
                raise exc
            return httpx.Response(status_code, json=response, request=httpx.Request("POST", url))

    return FakeAsyncClient


_SERVER_RESPONSE: dict = {
    "model": "d3:4b",
    "answers": {
        "salience": {
            "type": "score",
            "score": 1.667,
            "legend": {"0": "trivial smalltalk, safe to forget"},
            "probabilities": {"1": 0.5, "2": 0.3},
            "confidence": 0.209,
        },
        "keep": {"type": "noul", "noul": 0.1892},
        "mood": {
            "type": "choice",
            "choice": "positive",
            "probabilities": {"positive": 0.9731, "negative": 0.0093, "neutral": 0.0176},
            "confidence": 0.884,
        },
    },
    "usage": {"input_tokens": 383, "output_tokens": 0},
}


def test_decision_defaults_from_env_fallback():
    """Without providers.yaml, decisions fall back to the env-configured model."""
    config = _test_config()
    assert config.defaults.decision.model == "d3:4b"
    assert config.defaults.decision.provider == "default"
    assert "default" in config.providers


async def test_decide_builds_request_and_parses_answers():
    """decide() posts a System One payload and returns typed answers keyed by question name."""
    captured: list = []
    provider = Provider(_test_config())
    with patch(
        "backend.llm.provider.httpx.AsyncClient",
        _fake_systemone_client(captured, _SERVER_RESPONSE),
    ):
        answers = await provider.decide("hiking boots arrived this weekend", _questions())

    request = captured[0]
    assert request["url"] == "http://ollama.test/v1/systemone"
    assert request["headers"]["Authorization"].startswith("Bearer ")

    payload = request["json"]
    assert payload["model"] == "d3:4b"
    assert payload["state"] == "hiking boots arrived this weekend"
    assert set(payload["questions"]) == {"salience", "keep", "mood"}
    assert payload["questions"]["salience"] == {
        "type": "score",
        "instructions": "How salient is this memory for long-term recall about the user?",
        "criteria": [
            "trivial smalltalk, safe to forget",
            "mildly interesting context",
            "notable preference or fact about the user",
            "important milestone or strong preference",
            "critical life event",
        ],
    }
    # exclude_none drops the optional criteria of a noul question that sets none.
    bare = NoulQuestion(instructions="Is this relevant?")
    assert "criteria" not in bare.model_dump(exclude_none=True)

    assert answers["salience"].score == pytest.approx(1.667)
    assert answers["salience"].confidence == pytest.approx(0.209)
    assert answers["keep"].noul == pytest.approx(0.1892)
    assert answers["mood"].choice == "positive"
    assert answers["mood"].probabilities["positive"] == pytest.approx(0.9731)
    assert isinstance(answers["salience"], DecisionAnswer)


async def test_decide_passes_keep_alive():
    """An explicit keep_alive (batch rounds keep the model warm) reaches the payload."""
    captured: list = []
    provider = Provider(_test_config())
    with patch(
        "backend.llm.provider.httpx.AsyncClient",
        _fake_systemone_client(captured, _SERVER_RESPONSE),
    ):
        await provider.decide("state", {"keep": NoulQuestion(instructions="keep?")}, keep_alive="10m")
    assert captured[0]["json"]["keep_alive"] == "10m"


async def test_decide_rejects_empty_questions():
    provider = Provider(_test_config())
    with pytest.raises(DecisionError, match="at least one question"):
        await provider.decide("state", {})


async def test_decide_requires_every_answer():
    """A response missing one of the asked questions is an error, not a silent gap."""
    partial = {
        "model": "d3:4b",
        "answers": {"keep": _SERVER_RESPONSE["answers"]["keep"]},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    captured: list = []
    provider = Provider(_test_config())
    with patch(
        "backend.llm.provider.httpx.AsyncClient",
        _fake_systemone_client(captured, partial),
    ):
        with pytest.raises(DecisionError, match="missing answers for: \\['mood', 'salience'\\]"):
            await provider.decide("state", _questions())


async def test_decide_surfaces_http_errors():
    """Non-200 responses surface as DecisionError with status and body (ollama/gomodel
    report decision-API refusals as 400s)."""
    captured: list = []
    provider = Provider(_test_config())
    with patch(
        "backend.llm.provider.httpx.AsyncClient",
        _fake_systemone_client(captured, {"error": "d3:4b does not support chat"}, status_code=400),
    ):
        with pytest.raises(DecisionError, match="HTTP 400.*does not support chat"):
            await provider.decide("state", {"keep": NoulQuestion(instructions="keep?")})


async def test_decide_surfaces_network_errors():
    captured: list = []
    provider = Provider(_test_config())
    with patch(
        "backend.llm.provider.httpx.AsyncClient",
        _fake_systemone_client(captured, {}, exc=httpx.ConnectError("connection refused")),
    ):
        with pytest.raises(DecisionError, match="connection refused"):
            await provider.decide("state", {"keep": NoulQuestion(instructions="keep?")})


def test_question_models_validate_server_limits():
    """The question models mirror the System One limits client-side (score 2-10 levels,
    choice 2-255 options, noul criteria only false/true keys)."""
    with pytest.raises(ValidationError):
        ScoreQuestion(instructions="x", criteria=["only one level"])
    with pytest.raises(ValidationError):
        ScoreQuestion(instructions="x", criteria=[f"level {i}" for i in range(11)])
    with pytest.raises(ValidationError):
        ChoiceQuestion(instructions="x", criteria={"single": "one option"})
    with pytest.raises(ValidationError):
        NoulQuestion(instructions="x", criteria={"maybe": "not an outcome"})
    with pytest.raises(ValidationError):
        ChoiceQuestion.model_validate({"instructions": "x", "criteria": {"a": "x"}, "type": "noul"})
