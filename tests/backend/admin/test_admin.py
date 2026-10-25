from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from backend.database import get_async_sessionmaker
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import text


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


@pytest.fixture
def fake_langchain_client():
    with (
        patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings()),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model()),
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
