"""Tests for the hybrid search API and ranking formula evaluation."""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import (
    DEFAULT_RANKING_FORMULA,
    ChatGroupConfig,
    ChatGroupContext,
    ChatGroupCreate,
    ChatGroupCreateInternal,
    ChatGroupPrompts,
)
from backend.database import get_async_sessionmaker
from backend.llm import Provider
from backend.llm.provider import DecisionAnswer
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from backend.search.scoring import LINK_WEIGHT_REFERENCE, evaluate_ranking_formula, link_weight, normalize_bm25
from backend.user.models import UserDO
from httpx import AsyncClient
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import select


def _fake_embeddings():
    class FakeEmbeddings(OpenAIEmbeddings):
        def __init__(self, **kwargs):
            pass

        async def aembed_documents(
            self, texts: list[str], chunk_size: int | None = None, **kwargs: Any
        ) -> list[list[float]]:
            # i + 1 keeps every vector non-zero (a zero vector would make cosine distance NaN).
            return [[0.01 * (i + 1)] + [0.0] * 767 for i in range(len(texts))]

        async def aembed_query(self, text: str, **kwargs: Any) -> list[float]:
            return [0.01] + [0.0] * 767

    return FakeEmbeddings


def _fake_chat_model():
    class FakeChatModel(ChatOpenAI):
        def __init__(self, **kwargs):
            pass

        def bind(self, **kwargs):
            return self

        async def ainvoke(self, input: Any, config: Any = None, *, stop: Any = None, **kwargs: Any) -> Any:
            messages = input
            prompt = " ".join(getattr(m, "content", str(m)) for m in messages)
            if "events" in prompt:
                content = '{"events": [{"summary": "Demo event", "message_ids": []}]}'
            elif "topics" in prompt:
                content = '{"actions": [{"action": "create", "summary": "Demo topic"}]}'
            elif "facts" in prompt:
                content = '{"actions": [{"action": "create", "summary": "Demo fact"}]}'
            else:
                content = "{}"
            return SimpleNamespace(content=content)

    return FakeChatModel


def _fake_decide():
    """Decision calls return the top rubric level for messages (salience 1.0) but a
    mid level for memory objects (0.5): the event then keeps ranking below the plain
    messages through its lower salience, exercising the formula's salience factor."""

    async def decide(self, state, questions, task=None, keep_alive=None):
        score = 4.0 if "messages" in state else 2.0
        return {name: DecisionAnswer(type="score", score=score) for name in questions}

    return decide


@pytest.fixture
def fake_langchain_client():
    with (
        patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings()),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model()),
        patch.object(Provider, "decide", _fake_decide()),
    ):
        yield


async def login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/jwt/login", data={"username": email, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


async def _seed_search_data() -> dict:
    """Create a chatgroup with ten messages: three about hiking, seven about other topics."""
    async with get_async_sessionmaker()() as session, session.begin():
        user = await session.scalar(select(UserDO).where(UserDO.email == "test@example.com"))
        chatgroup = ChatGroupDO(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Search Group",
            config=ChatGroupConfig(protected_message_count=0).model_dump(),
            prompts=ChatGroupPrompts().model_dump(),
            ranking_formula=DEFAULT_RANKING_FORMULA,
        )
        session.add(chatgroup)
        await session.flush()
        chat = ChatDO(id=uuid.uuid4(), chatgroup_id=chatgroup.id, name="Search Chat")
        session.add(chat)
        await session.flush()
        texts = [
            "I went hiking in the mountains last weekend.",
            "The hiking trail was steep but the views were amazing.",
            "My favorite hiking boots are finally broken in.",
            "We cooked pasta on a portable stove at the campsite.",
            "Next month I want to try rock climbing.",
            "The dog learned to fetch the newspaper every morning.",
            "I started learning Japanese with an app.",
            "My grandmother's soup recipe needs fresh thyme.",
            "The new office has standing desks and a quiet room.",
            "I repainted the balcony fence in dark green.",
        ]
        for text in texts:
            session.add(
                MessageDO(
                    id=uuid.uuid4(),
                    chat_id=chat.id,
                    full_text=text,
                    role="user",
                    processing_state={
                        "embedding": ProcessingStatus.WAITING,
                        "salience": ProcessingStatus.WAITING,
                        "event": ProcessingStatus.WAITING,
                        "topic": ProcessingStatus.WAITING,
                        "fact": ProcessingStatus.WAITING,
                    },
                )
            )
    return {"chatgroup_id": chatgroup.id}


def test_default_ranking_formula_is_a_pydantic_default():
    """Every create schema inherits the shared formula: no caller has to pass one."""
    assert ChatGroupCreate(name="Group").ranking_formula == DEFAULT_RANKING_FORMULA
    internal = ChatGroupCreateInternal(
        name="Group",
        user_id=uuid.uuid4(),
        context=ChatGroupContext(user_id=uuid.uuid4()),
    )
    assert internal.ranking_formula == DEFAULT_RANKING_FORMULA


def test_evaluate_ranking_formula():
    """The default formula takes the max of the (normalized) channels, weighted by salience
    and by the logarithmic link weight."""
    # An unlinked item is not disqualified any more: it keeps half weight.
    assert evaluate_ranking_formula(DEFAULT_RANKING_FORMULA, 0.8, 0.4, 1.0, 0) == pytest.approx(0.4)
    # 100 links saturate the weight to 1.0.
    assert evaluate_ranking_formula(DEFAULT_RANKING_FORMULA, 0.2, 0.9, 1.0, LINK_WEIGHT_REFERENCE) == pytest.approx(0.9)
    assert evaluate_ranking_formula(DEFAULT_RANKING_FORMULA, 0.8, 0.4, 0.5, 3) == pytest.approx(
        0.8 * 0.5 * link_weight(3)
    )


def test_ranking_formula_can_use_math_helpers():
    """`link_weight`, `log10` and friends are available to admin-authored formulas."""
    formula = (
        "lambda vector_score, bm25_score, salience, linked_count: log10(1 + bm25_score) + link_weight(linked_count)"
    )
    assert evaluate_ranking_formula(formula, 0.0, 9.0, 1.0, 0) == pytest.approx(1.0 + 0.5)


def test_evaluate_ranking_formula_rejects_non_lambda():
    with pytest.raises(ValueError, match="lambda"):
        evaluate_ranking_formula("__import__('os').getcwd()", 0.0, 0.0, 1.0, 0)


def test_ranking_formula_boolean_and_conditional_semantics():
    """Formulas follow plain Python semantics for comparisons, and/or and conditionals."""
    formula = (
        "lambda vector_score, bm25_score, salience, linked_count: "
        "(vector_score > 0.5 and bm25_score) or salience * link_weight(linked_count)"
    )
    evaluate = evaluate_ranking_formula
    # vector_score > 0.5 -> take the bm25_score branch
    assert evaluate(formula, 0.8, 0.4, 0.25, 3) == pytest.approx(0.4)
    # vector_score <= 0.5 -> and short-circuits to False, or falls through to the salience branch
    assert evaluate(formula, 0.2, 0.4, 0.25, 3) == pytest.approx(0.25 * link_weight(3))
    # chained comparison and conditional expression
    formula = "lambda vector_score, bm25_score, salience, linked_count: bm25_score if 0 < linked_count <= 10 else 0.0"
    assert evaluate(formula, 0, 0.7, 1.0, 3) == pytest.approx(0.7)
    assert evaluate(formula, 0, 0.7, 1.0, 11) == pytest.approx(0.0)


def test_normalize_bm25():
    """BM25 relevance is unbounded, so it is scaled against the best hit of the query."""
    assert normalize_bm25(0.0, 0.0) == 0.0
    assert normalize_bm25(0.0, 6.42) == 0.0
    assert normalize_bm25(6.42, 6.42) == pytest.approx(1.0)
    assert normalize_bm25(3.21, 6.42) == pytest.approx(0.5)
    assert normalize_bm25(12.0, 6.42) == pytest.approx(1.0)  # clamped


def test_link_weight_is_logarithmic():
    """0 links -> 0.5, 100 links -> 1.0, monotonically increasing in between."""
    assert link_weight(0) == pytest.approx(0.5)
    assert link_weight(LINK_WEIGHT_REFERENCE) == pytest.approx(1.0)
    assert link_weight(0) < link_weight(1) < link_weight(10) < link_weight(100)
    # Negative counts are nonsense but must not blow the log up.
    assert link_weight(-5) == pytest.approx(0.5)


@pytest.mark.usefixtures("fake_langchain_client")
async def test_search_endpoint_hybrid_ranking(client: AsyncClient):
    """Search must merge BM25 and vector candidates, then rank them with the chatgroup formula."""
    ids = await _seed_search_data()
    # Round 1 embeds and scores the messages and creates a topic and a fact; round 2
    # embeds and scores those memory objects.
    assert (await client.post("/admin/batch/trigger")).status_code == 200
    assert (await client.post("/admin/batch/trigger")).status_code == 200

    token = await login(client, "test@example.com", "devpassword123")
    response = await client.get(
        f"/api/v1/chatgroups/{ids['chatgroup_id']}/search",
        params={"q": "hiking"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["q"] == "hiking"

    hits = data["hits"]
    scores = [hit["score"] for hit in hits]
    assert scores == sorted(scores, reverse=True)

    types = {hit["type"] for hit in hits}
    assert {"message", "event", "topic", "fact"} <= types

    message_hits = [hit for hit in hits if hit["type"] == "message"]
    assert len(message_hits) == 10
    assert sum(hit["bm25_score"] > 0 for hit in message_hits) == 3
    assert all(hit["vector_score"] > 0 for hit in message_hits)

    # Both channels are normalized to 0..1: the fake vectors are all collinear with the
    # query (vector_score 1.0) and the best BM25 match is scaled to 1.0.
    assert all(0.0 <= hit["bm25_score"] <= 1.0 for hit in hits)
    assert all(0.0 <= hit["vector_score"] <= 1.0 for hit in hits)
    assert max(hit["bm25_score"] for hit in message_hits) == pytest.approx(1.0)

    # The two consolidated messages (event + topic + fact links) rank first: full score on
    # both channels, full salience, and a higher link weight than the plain messages.
    # All ten messages share one created_at, so which two got the topic/fact links is
    # decided by id tie-breaking, but the counts and scores are deterministic.
    linked_messages = [hit for hit in message_hits if hit["linked_count"] == 3]
    assert len(linked_messages) == 2
    assert hits[0]["type"] == "message"
    assert all(hit["score"] == pytest.approx(link_weight(3)) for hit in linked_messages)

    # Plain messages (one event link each, salience 1.0 from the fake decision call) follow.
    plain_messages = [hit for hit in message_hits if hit["linked_count"] == 1]
    assert len(plain_messages) == 8
    assert all(hit["score"] == pytest.approx(link_weight(1)) for hit in plain_messages)

    # The event spans all ten block messages (10 links) but the fake decision call scored
    # it at 0.5 (mid rubric level), which halves its score below every plain message.
    event_hit = next(hit for hit in hits if hit["type"] == "event")
    assert event_hit["linked_count"] == 10
    assert event_hit["salience"] == pytest.approx(0.5)
    assert event_hit["bm25_score"] == 0.0
    assert event_hit["score"] == pytest.approx(0.5 * link_weight(10))
    assert event_hit["score"] < plain_messages[0]["score"]


@pytest.mark.usefixtures("fake_langchain_client")
async def test_search_endpoint_respects_k(client: AsyncClient):
    ids = await _seed_search_data()
    assert (await client.post("/admin/batch/trigger")).status_code == 200
    assert (await client.post("/admin/batch/trigger")).status_code == 200

    token = await login(client, "test@example.com", "devpassword123")
    response = await client.get(
        f"/api/v1/chatgroups/{ids['chatgroup_id']}/search",
        params={"q": "hiking", "k": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert len(response.json()["hits"]) == 2


async def test_search_endpoint_requires_auth(client: AsyncClient):
    ids = await _seed_search_data()
    response = await client.get(f"/api/v1/chatgroups/{ids['chatgroup_id']}/search", params={"q": "hiking"})
    assert response.status_code == 401


@pytest.mark.usefixtures("fake_langchain_client")
async def test_search_endpoint_survives_query_syntax(client: AsyncClient):
    """The BM25 channel runs the query through ParadeDB's query-string parser: raw Lucene
    syntax (unbalanced quotes, field selectors, boolean operators, escapes, wildcards)
    must be escaped, and blank queries rejected, instead of raising 500s."""
    ids = await _seed_search_data()
    assert (await client.post("/admin/batch/trigger")).status_code == 200
    assert (await client.post("/admin/batch/trigger")).status_code == 200

    token = await login(client, "test@example.com", "devpassword123")
    for q in ['a"b', "field:value", "boots AND hiking", "h~2", "\\", "NOT", "hiking!", "boots*"]:
        response = await client.get(
            f"/api/v1/chatgroups/{ids['chatgroup_id']}/search",
            params={"q": q},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, q
        assert all(hit["score"] == hit["score"] for hit in response.json()["hits"])  # no NaN scores

    # Escaping does not change plain matching: a simple term still finds its messages.
    response = await client.get(
        f"/api/v1/chatgroups/{ids['chatgroup_id']}/search",
        params={"q": "hiking"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert len([hit for hit in response.json()["hits"] if hit["type"] == "message"]) == 10

    # Blank queries never reach the parser.
    for q in ["", "   "]:
        response = await client.get(
            f"/api/v1/chatgroups/{ids['chatgroup_id']}/search",
            params={"q": q},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, q


async def test_create_chatgroup_accepts_custom_ranking_formula(client: AsyncClient):
    """A chatgroup owner can store their own formula; it is only evaluated by search.

    NOTE: formulas are evaluated with `eval` (see `plan/issue.md`, issue 001): a hostile
    formula is not rejected on write. Do not open chatgroups to untrusted users until
    that issue is fixed.
    """
    token = await login(client, "test@example.com", "devpassword123")
    response = await client.post(
        "/api/v1/chatgroups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Group",
            "ranking_formula": "lambda vector_score, bm25_score, salience, linked_count: vector_score",
        },
    )
    assert response.status_code == 201
    assert response.json()["ranking_formula"].startswith("lambda vector_score")
