"""Tests for the hybrid search API, ranking formula evaluation and token-budget packing."""

import contextlib
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import backend.search.formulas as formula_module
import pytest
from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import (
    ChatGroupConfig,
    ChatGroupContext,
    ChatGroupCreate,
    ChatGroupCreateInternal,
    ChatGroupPrompts,
)
from backend.database import get_async_sessionmaker
from backend.llm import Provider
from backend.llm.provider import DecisionAnswer, DecisionError
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from backend.search.formulas import DEFAULT_RANKING_FORMULA, DEFAULT_RANKING_FORMULA_NAME
from backend.search.scoring import LINK_WEIGHT_REFERENCE, evaluate_ranking_formula, link_weight, normalize_bm25
from backend.user.models import UserDO
from backend.utils.embeddings import embed_texts
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
            lowered = prompt.lower()
            if "fact_id" in lowered:
                content = '{"actions": [{"action": "create", "summary": "Demo fact"}]}'
            elif "topic_id" in lowered:
                content = '{"actions": [{"action": "create", "summary": "Demo topic"}]}'
            elif "event" in lowered:
                content = '{"summary": "Demo event"}'
            else:
                content = "{}"
            return SimpleNamespace(content=content)

    return FakeChatModel


def _fake_decide(
    ratio_probabilities: dict[str, float] | None = None,
    calls: list[dict[str, Any]] | None = None,
    error: bool = False,
    omit_probabilities: bool = False,
):
    """Decision calls return the top rubric level for messages (salience 1.0) but a
    mid level for memory objects (0.5): the event then keeps ranking below the plain
    messages through its lower salience, exercising the formula's salience factor.

    A `memory_ratio` choice question is answered with `ratio_probabilities` (the
    config-ratio defaults when None), without any probabilities when
    `omit_probabilities` is set, or raised as DecisionError when `error` is set.
    `calls` records every call when given."""

    async def decide(self, state, questions, task=None, keep_alive=None):
        if calls is not None:
            calls.append({"state": state, "questions": questions, "task": task})
        answers = {}
        for name, question in questions.items():
            if name == "memory_ratio":
                if error:
                    raise DecisionError("decision model unavailable")
                probabilities = None
                if not omit_probabilities:
                    probabilities = ratio_probabilities or {"message": 0.4, "event": 0.1, "topic": 0.3, "fact": 0.2}
                answers[name] = DecisionAnswer(type="choice", choice="message", probabilities=probabilities)
            elif name == "boundary":
                # Every message extends the last event: the seeded chat keeps a single
                # event spanning all its messages.
                probabilities = {key: 0.0 for key in question.criteria}
                probabilities["extend"] = 1.0
                answers[name] = DecisionAnswer(type="choice", choice="extend", probabilities=probabilities)
            else:
                score = 4.0 if "message" in state else 2.0
                answers[name] = DecisionAnswer(type="score", score=score)
        return answers

    return decide


@contextlib.contextmanager
def _fake_provider(
    ratio_probabilities: dict[str, float] | None = None,
    calls: list[dict[str, Any]] | None = None,
    error: bool = False,
    omit_probabilities: bool = False,
):
    """Patch the LangChain wiring and the decision calls for the wrapped block."""
    with (
        patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings()),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model()),
        patch.object(Provider, "decide", _fake_decide(ratio_probabilities, calls, error, omit_probabilities)),
    ):
        yield


@pytest.fixture
def fake_langchain_client():
    with _fake_provider():
        yield


async def login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/jwt/login", data={"username": email, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


async def _seed_search_data(
    use_llm_memory_ratio: bool = False,
    prompts: dict[str, Any] | None = None,
    ranking_formula: str = DEFAULT_RANKING_FORMULA_NAME,
) -> dict:
    """Create a chatgroup with ten messages: three about hiking, seven about other
    topics. `prompts` updates individual prompts keys on top of the schema defaults."""
    chat_prompts = ChatGroupPrompts().model_dump()
    chat_prompts.update(prompts or {})
    async with get_async_sessionmaker()() as session, session.begin():
        user = await session.scalar(select(UserDO).where(UserDO.email == "test@example.com"))
        chatgroup = ChatGroupDO(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Search Group",
            config=ChatGroupConfig(protected_message_count=0, use_llm_memory_ratio=use_llm_memory_ratio).model_dump(),
            prompts=chat_prompts,
            ranking_formula=ranking_formula,
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
        added_messages: list[MessageDO] = []
        for text in texts:
            message = MessageDO(
                id=uuid.uuid4(),
                chat_id=chat.id,
                full_text=text,
                role="user",
                processing_state={
                    "event": ProcessingStatus.WAITING,
                    "topic": ProcessingStatus.WAITING,
                    "fact": ProcessingStatus.WAITING,
                },
            )
            session.add(message)
            added_messages.append(message)

        provider = Provider()
        embeddings = await embed_texts([message.full_text for message in added_messages], provider)
        for message, vector in zip(added_messages, embeddings):
            message.embedding = vector

    return {"chatgroup_id": chatgroup.id}


@contextlib.contextmanager
def _registered_formulas(**entries: str):
    """Temporarily extend the formula registry with named entries."""
    registry = dict(formula_module.get_ranking_formulas())
    registry.update(entries)
    with patch.object(formula_module, "_formulas", registry):
        yield


def test_default_ranking_formula_is_a_pydantic_default():
    """Every create schema inherits the shared formula name: no caller has to pass one."""
    assert ChatGroupCreate(name="Group").ranking_formula == DEFAULT_RANKING_FORMULA_NAME
    internal = ChatGroupCreateInternal(
        name="Group",
        user_id=uuid.uuid4(),
        context=ChatGroupContext(user_id=uuid.uuid4()),
    )
    assert internal.ranking_formula == DEFAULT_RANKING_FORMULA_NAME


def test_evaluate_ranking_formula():
    """The default formula takes the max of the (normalized) channels, weighted by salience
    and by the logarithmic link weight."""
    # An unlinked item keeps half weight instead of being disqualified.
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
    # Round 1 embeds and scores the messages and creates the first topics and facts;
    # round 2 embeds and scores those memory objects. Topic and fact stages process
    # five messages per round, so after two rounds every message carries a topic and
    # a fact link besides its event link.
    assert (await client.post("/admin/batch/trigger")).status_code == 200
    assert (await client.post("/admin/batch/trigger")).status_code == 200

    token = await login(client, "test@example.com", "devpassword123")
    # Two rounds create 10 messages, 10 topics, 10 facts and one event. The default
    # k=20 would exclude the event because it scores below every message/topic/fact,
    # so ask for enough hits to see all memory types.
    response = await client.get(
        f"/api/v1/chatgroups/{ids['chatgroup_id']}/search",
        params={"q": "hiking", "k": 50},
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

    # Every message carries all three links (event + topic + fact): full score on both
    # channels, full salience, and the three-link weight. The plain-message case
    # (linked_count 1) is gone — the consolidation stages covered all ten messages.
    linked_messages = [hit for hit in message_hits if hit["linked_count"] == 3]
    assert len(linked_messages) == 10
    assert hits[0]["type"] == "message"
    assert all(hit["score"] == pytest.approx(link_weight(3)) for hit in linked_messages)

    # The single event links all ten messages (the fake boundary decision extends it
    # for every message). The fake decision call scored it at 0.5 (mid rubric level),
    # which halves its score below every message.
    event_hit = next(hit for hit in hits if hit["type"] == "event")
    assert event_hit["linked_count"] == 10
    assert event_hit["salience"] == pytest.approx(0.5)
    assert event_hit["bm25_score"] == 0.0
    assert event_hit["score"] == pytest.approx(0.5 * link_weight(10))
    assert event_hit["score"] < linked_messages[0]["score"]


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


async def test_create_chatgroup_accepts_named_ranking_formula(client: AsyncClient):
    """A chatgroup stores the *name* of a formula registered in the settings file."""
    vector_only = "lambda vector_score, bm25_score, salience, linked_count: vector_score"
    token = await login(client, "test@example.com", "devpassword123")
    with _registered_formulas(vector_only=vector_only):
        response = await client.post(
            "/api/v1/chatgroups",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "Group", "ranking_formula": "vector_only"},
        )
    assert response.status_code == 201
    assert response.json()["ranking_formula"] == "vector_only"


async def test_create_chatgroup_rejects_unknown_ranking_formula(client: AsyncClient):
    """The API accepts formula names only, never formula source: an undefined name and
    the classic restricted-namespace sandbox escape are both rejected on write."""
    token = await login(client, "test@example.com", "devpassword123")
    payloads = (
        {"name": "Group", "ranking_formula": "gone"},
        {
            "name": "Group",
            "ranking_formula": (
                "lambda vector_score, bm25_score, salience, linked_count: ().__class__.__base__.__subclasses__()"
            ),
        },
    )
    for payload in payloads:
        response = await client.post(
            "/api/v1/chatgroups",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        assert response.status_code == 422


async def test_search_scores_with_the_named_formula(client: AsyncClient):
    """Search resolves the chatgroup's formula name against the registry before
    scoring: a registered `vector_only` formula is what the evaluator receives."""
    ids = await _seed_search_data(ranking_formula="vector_only")
    vector_only = "lambda vector_score, bm25_score, salience, linked_count: vector_score"
    recorded: dict[str, str] = {}

    def _recording_eval(formula: str, *args: Any) -> float:
        recorded["formula"] = formula
        return evaluate_ranking_formula(formula, *args)

    with _registered_formulas(vector_only=vector_only), _fake_provider():
        assert (await client.post("/admin/batch/trigger")).status_code == 200
        assert (await client.post("/admin/batch/trigger")).status_code == 200

        token = await login(client, "test@example.com", "devpassword123")
        with patch("backend.search.service.evaluate_ranking_formula", side_effect=_recording_eval):
            data = await _search(client, token, ids["chatgroup_id"], q="hiking")

    assert recorded["formula"] == vector_only
    assert len(data["hits"]) > 0


async def test_search_rejects_unknown_formula_name(client: AsyncClient):
    """A chatgroup whose stored formula name is not defined fails the search with a
    422 problem instead of silently scoring with another formula."""
    ids = await _seed_search_data(ranking_formula="gone")
    with _fake_provider():
        token = await login(client, "test@example.com", "devpassword123")
        response = await client.get(
            f"/api/v1/chatgroups/{ids['chatgroup_id']}/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": "hiking"},
        )
    assert response.status_code == 422
    assert "gone" in response.json()["detail"]


def _hit_tokens(data: dict) -> dict[str, int]:
    """Whitespace tokens of the returned hits, per type, as the response reports them."""
    used = {"message": 0, "event": 0, "topic": 0, "fact": 0}
    for hit in data["hits"]:
        used[hit["type"]] += hit["tokens"]
    return used


def _type_counts(data: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for hit in data["hits"]:
        counts[hit["type"]] = counts.get(hit["type"], 0) + 1
    return counts


CONFIG_RATIO = {"message": 0.40, "event": 0.10, "topic": 0.30, "fact": 0.20}


async def _search(client: AsyncClient, token: str, chatgroup_id: uuid.UUID, **params) -> dict:
    response = await client.get(
        f"/api/v1/chatgroups/{chatgroup_id}/search",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.usefixtures("fake_langchain_client")
async def test_search_packs_token_budget(client: AsyncClient):
    """A small `token_budget` packs the hits per type share: the best hit of each type
    is kept (even over its share), lower-ranked hits of the type are dropped once the
    share is exhausted, and the budget/ratio bookkeeping is reported."""
    ids = await _seed_search_data()
    assert (await client.post("/admin/batch/trigger")).status_code == 200
    assert (await client.post("/admin/batch/trigger")).status_code == 200

    token = await login(client, "test@example.com", "devpassword123")
    # A generous budget keeps every packed hit up to the default k: ten messages, one
    # event, and one topic and one fact per message (the consolidation stages cover
    # five messages per round, so two rounds link all ten).
    full = await _search(client, token, ids["chatgroup_id"], q="hiking")
    assert full["token_budget"] == 2000  # the chatgroup config default
    assert full["ratio_source"] == "config"
    assert full["memory_ratio"] == CONFIG_RATIO
    assert len(full["hits"]) == 20  # 31 candidates, capped by the default k=20

    # Budget 8 with the config ratio gives tiny shares (message 3.2, event 0.8,
    # topic 2.4, fact 1.6 tokens): the event and fact exceed their shares and the best
    # message dwarfs its own, so each type survives only through the best-hit rule;
    # no second message fits afterwards.
    packed = await _search(client, token, ids["chatgroup_id"], q="hiking", token_budget=8)
    assert packed["token_budget"] == 8
    assert packed["ratio_source"] == "config"
    assert packed["memory_ratio"] == CONFIG_RATIO
    assert _type_counts(packed) == {"message": 1, "event": 1, "topic": 1, "fact": 1}
    scores = [hit["score"] for hit in packed["hits"]]
    assert scores == sorted(scores, reverse=True)
    # tokens_used matches the returned hits per type; the total exceeds the budget
    # only through the best-hit inclusions.
    assert packed["tokens_used"] == _hit_tokens(packed)
    assert sum(packed["tokens_used"].values()) > 8


@pytest.mark.usefixtures("fake_langchain_client")
async def test_search_token_budget_validation(client: AsyncClient):
    """`token_budget` must be a positive integer when given."""
    ids = await _seed_search_data()
    token = await login(client, "test@example.com", "devpassword123")
    response = await client.get(
        f"/api/v1/chatgroups/{ids['chatgroup_id']}/search",
        params={"q": "hiking", "token_budget": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


async def test_search_llm_memory_ratio_uses_decision_model(client: AsyncClient):
    """With `use_llm_memory_ratio` the ratio comes from one decision-model choice
    question: the answer's probabilities are the distribution (renormalized, missing
    types at zero) and drive the per-type packing."""
    ids = await _seed_search_data(use_llm_memory_ratio=True)
    calls: list[dict[str, Any]] = []
    with _fake_provider(ratio_probabilities={"message": 4.0, "topic": 1.0}, calls=calls):
        assert (await client.post("/admin/batch/trigger")).status_code == 200
        assert (await client.post("/admin/batch/trigger")).status_code == 200
        calls.clear()  # salience scoring is done; what follows is the search itself

        token = await login(client, "test@example.com", "devpassword123")
        data = await _search(client, token, ids["chatgroup_id"], q="hiking", token_budget=10)

    ratio_calls = [call for call in calls if "memory_ratio" in call["questions"]]
    assert len(ratio_calls) == 1
    question = ratio_calls[0]["questions"]["memory_ratio"]
    assert ratio_calls[0]["task"] == "memory_ratio"
    assert ratio_calls[0]["state"]["query"] == "hiking"
    assert question.type == "choice"
    assert set(question.criteria) == {"message", "event", "topic", "fact"}

    # 4:1 renormalizes to 0.8/0.2; event and fact get no share, so their hits are
    # dropped even though both exist and match.
    assert data["ratio_source"] == "decision"
    assert data["memory_ratio"] == {"message": 0.8, "topic": 0.2, "event": 0.0, "fact": 0.0}
    assert data["token_budget"] == 10
    assert _type_counts(data) == {"message": 1, "topic": 1}
    assert data["tokens_used"] == _hit_tokens(data)


async def test_search_llm_memory_ratio_falls_back_to_config(client: AsyncClient):
    """A failing or empty ratio decision falls back to the config ratio and the
    search still succeeds."""
    ids = await _seed_search_data(use_llm_memory_ratio=True)
    for error, omit in [(True, False), (False, True)]:
        with _fake_provider(error=error, omit_probabilities=omit):
            assert (await client.post("/admin/batch/trigger")).status_code == 200
            token = await login(client, "test@example.com", "devpassword123")
            data = await _search(client, token, ids["chatgroup_id"], q="hiking")

        assert data["ratio_source"] == "config"
        assert data["memory_ratio"] == CONFIG_RATIO
        assert len(data["hits"]) > 0


async def test_search_uses_configured_memory_ratio_criteria(client: AsyncClient):
    """The criteria of the ratio choice question come from the chatgroup's
    `memory_ratio_criteria` prompts key: custom descriptions are passed through, and
    types left out of the criteria are offered no share (their hits are dropped)."""
    ids = await _seed_search_data(
        use_llm_memory_ratio=True,
        prompts={"memory_ratio_criteria": {"message": "Raw conversation words.", "topic": "Recurring themes."}},
    )
    calls: list[dict[str, Any]] = []
    with _fake_provider(ratio_probabilities={"message": 3.0, "topic": 1.0}, calls=calls):
        assert (await client.post("/admin/batch/trigger")).status_code == 200
        assert (await client.post("/admin/batch/trigger")).status_code == 200
        calls.clear()  # salience scoring is done; what follows is the search itself

        token = await login(client, "test@example.com", "devpassword123")
        data = await _search(client, token, ids["chatgroup_id"], q="hiking", token_budget=10)

    ratio_calls = [call for call in calls if "memory_ratio" in call["questions"]]
    assert len(ratio_calls) == 1
    question = ratio_calls[0]["questions"]["memory_ratio"]
    assert question.criteria == {"message": "Raw conversation words.", "topic": "Recurring themes."}

    # The model can only spread probability over the offered criteria, so event and
    # fact get no share and their hits are dropped even though both exist and match.
    assert data["ratio_source"] == "decision"
    assert data["memory_ratio"] == {"message": 0.75, "topic": 0.25, "event": 0.0, "fact": 0.0}
    assert _type_counts(data) == {"message": 1, "topic": 1}
    assert data["tokens_used"] == _hit_tokens(data)
