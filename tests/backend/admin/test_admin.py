from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO
from backend.fact.models import FactDO
from backend.llm import Provider
from backend.llm.provider import DecisionAnswer
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from backend.topic.models import TopicDO
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import select, text


def _fake_embeddings():
    class FakeEmbeddings(OpenAIEmbeddings):
        def __init__(self, **kwargs):
            pass

        async def aembed_documents(
            self, texts: list[str], chunk_size: int | None = None, **kwargs: Any
        ) -> list[list[float]]:
            return [[0.01 * i] + [0.0] * 767 for i in range(len(texts))]

        async def aembed_query(self, text: str, **kwargs: Any) -> list[float]:
            return [0.0] * 768

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


@pytest.fixture(autouse=True)
async def clean_memory_tables():
    async with get_async_sessionmaker()() as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE TABLE chatgroup, chat, message, event, topic, fact, "
                "event_message_links, topic_message_links, fact_message_links "
                "RESTART IDENTITY CASCADE"
            )
        )


def _fake_decide():
    async def decide(self, state, questions, task=None, keep_alive=None):
        return {name: DecisionAnswer(type="score", score=2.0) for name in questions}

    return decide


@pytest.fixture
def fake_langchain_client():
    with (
        patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings()),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model()),
        patch.object(Provider, "decide", _fake_decide()),
    ):
        yield


async def test_seed_demo_endpoint(client):
    response = await client.post("/admin/seed/demo")
    assert response.status_code == 200
    data = response.json()
    assert data["message_count"] == 30
    assert data["chatgroup_id"] is not None
    assert data["chat_id"] is not None


async def test_batch_status_endpoint(client):
    response = await client.get("/admin/batch/status")
    assert response.status_code == 200
    data = response.json()
    assert data["running"] is False


@pytest.mark.usefixtures("fake_langchain_client")
async def test_batch_trigger_endpoint(client):
    await client.post("/admin/seed/demo")
    response = await client.post("/admin/batch/trigger")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.usefixtures("fake_langchain_client")
async def test_reembed_endpoint(client):
    """After consolidation, reembed must null every vector and requeue the embedding
    stage so the worker recomputes everything with the (new) embedding model."""
    await client.post("/admin/seed/demo")
    assert (await client.post("/admin/batch/trigger")).status_code == 200

    response = await client.post("/admin/reembed")
    assert response.status_code == 200
    data = response.json()
    assert data["messages"] == 30
    assert data["events"] >= 1
    assert data["topics"] >= 1
    assert data["facts"] >= 1

    async with get_async_sessionmaker()() as session:
        messages = list((await session.execute(select(MessageDO))).scalars().all())
        assert len(messages) == 30
        for message in messages:
            assert message.embedding is None
            assert message.processing_state["embedding"] == ProcessingStatus.WAITING
        for model in (EventDO, TopicDO, FactDO):
            objects = list((await session.execute(select(model))).scalars().all())
            assert all(obj.embedding is None for obj in objects)
