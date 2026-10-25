import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from backend.batch import BatchWorker, batch_status
from backend.batch.embeddings import embed_memory_objects, embed_messages
from backend.batch.pipelines import EventPipeline, FactPipeline, TopicPipeline
from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupConfig, ChatGroupPrompts
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO, EventMessageLinkDO
from backend.fact.models import FactDO, FactMessageLinkDO
from backend.message.constants import MessageRole, ProcessingStatus
from backend.message.models import MessageDO
from backend.seeders import seed_demo_chat
from backend.topic.models import TopicDO, TopicMessageLinkDO
from backend.user.models import UserDO
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import select, text


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


def _fake_embeddings():
    """Return a LangChain OpenAIEmbeddings whose aembed_documents returns deterministic vectors."""

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
    """Return a LangChain ChatOpenAI whose ainvoke returns deterministic JSON."""

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


@pytest.fixture
def fake_langchain_client():
    with (
        patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings()),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model()),
    ):
        yield


async def _make_test_chat(session):
    user = await session.scalar(select(UserDO).where(UserDO.email == "test@example.com"))
    assert user is not None
    chatgroup = ChatGroupDO(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Test ChatGroup",
        config=ChatGroupConfig().model_dump(),
        prompts=ChatGroupPrompts().model_dump(),
        ranking_formula="lambda vector_score, bm25_score, salience, linked_count: vector_score",
    )
    session.add(chatgroup)
    await session.flush()
    chat = ChatDO(
        id=uuid.uuid4(),
        chatgroup_id=chatgroup.id,
        name="Test Chat",
    )
    session.add(chat)
    await session.flush()
    return chatgroup, chat


async def _add_message(session, chat_id, full_text, role=MessageRole.USER, prev_id=None, state=None):
    message = MessageDO(
        id=uuid.uuid4(),
        chat_id=chat_id,
        prev_message_id=prev_id,
        full_text=full_text,
        role=role,
        embedding=None,
        processing_state=state
        or {
            "embedding": ProcessingStatus.WAITING,
            "salience": ProcessingStatus.DONE,
            "event": ProcessingStatus.WAITING,
            "topic": ProcessingStatus.WAITING,
            "fact": ProcessingStatus.WAITING,
            "last_failure_at": None,
            "retry_count": 0,
        },
    )
    session.add(message)
    await session.flush()
    return message


@pytest.mark.usefixtures("fake_langchain_client")
async def test_embed_messages():
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        message = await _add_message(session, chat.id, "Hello world")

    embedded = await embed_messages()
    assert embedded == 1

    async with get_async_sessionmaker()() as session, session.begin():
        updated = await session.get(MessageDO, message.id)
        assert updated is not None
        assert updated.embedding is not None
        assert len(updated.embedding) == 768
        assert updated.processing_state["embedding"] == ProcessingStatus.DONE


@pytest.mark.usefixtures("fake_langchain_client")
async def test_embed_memory_objects():
    async with get_async_sessionmaker()() as session, session.begin():
        chatgroup, chat = await _make_test_chat(session)
        event = EventDO(
            chat_id=chat.id,
            full_text="Test event",
            start_at=chat.created_at,
            end_at=chat.created_at,
        )
        session.add(event)

    embedded = await embed_memory_objects()
    assert embedded == 1

    async with get_async_sessionmaker()() as session, session.begin():
        updated = await session.get(EventDO, event.id)
        assert updated is not None
        assert updated.embedding is not None
        assert len(updated.embedding) == 768


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_pipeline():
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        messages = []
        prev = None
        for i in range(3):
            msg = await _add_message(session, chat.id, f"Event message {i}", prev_id=prev)
            msg.embedding = [0.0] * 768
            messages.append(msg)
            prev = msg.id

    processed = await EventPipeline.run_round()
    assert processed == 3

    async with get_async_sessionmaker()() as session, session.begin():
        events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().all())
        assert len(events) == 1
        assert events[0].full_text == "Demo event"
        links = list(
            (await session.execute(select(EventMessageLinkDO).where(EventMessageLinkDO.event_id == events[0].id)))
            .scalars()
            .all()
        )
        assert len(links) == 3
        for msg in messages:
            updated = await session.get(MessageDO, msg.id)
            assert updated.processing_state["event"] == ProcessingStatus.DONE


@pytest.mark.usefixtures("fake_langchain_client")
async def test_topic_pipeline():
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        msg = await _add_message(session, chat.id, "I love hiking in the mountains")
        msg.embedding = [0.0] * 768

    processed = await TopicPipeline.run_round()
    assert processed == 1

    async with get_async_sessionmaker()() as session, session.begin():
        topics = list(
            (await session.execute(select(TopicDO).where(TopicDO.chatgroup_id == chat.chatgroup_id))).scalars().all()
        )
        assert len(topics) == 1
        assert topics[0].full_text == "Demo topic"
        links = list(
            (await session.execute(select(TopicMessageLinkDO).where(TopicMessageLinkDO.topic_id == topics[0].id)))
            .scalars()
            .all()
        )
        assert len(links) == 1


@pytest.mark.usefixtures("fake_langchain_client")
async def test_fact_pipeline():
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        msg = await _add_message(session, chat.id, "My favorite color is blue")
        msg.embedding = [0.0] * 768

    processed = await FactPipeline.run_round()
    assert processed == 1

    async with get_async_sessionmaker()() as session, session.begin():
        facts = list(
            (await session.execute(select(FactDO).where(FactDO.chatgroup_id == chat.chatgroup_id))).scalars().all()
        )
        assert len(facts) == 1
        assert facts[0].full_text == "Demo fact"
        links = list(
            (await session.execute(select(FactMessageLinkDO).where(FactMessageLinkDO.fact_id == facts[0].id)))
            .scalars()
            .all()
        )
        assert len(links) == 1


@pytest.mark.usefixtures("fake_langchain_client")
async def test_batch_worker_round():
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        msg = await _add_message(session, chat.id, "Batch round test message")
        msg.embedding = [0.0] * 768

    worker = BatchWorker()
    await worker.run_round()

    assert batch_status.running is False
    async with get_async_sessionmaker()() as session, session.begin():
        topics = list(
            (await session.execute(select(TopicDO).where(TopicDO.chatgroup_id == chat.chatgroup_id))).scalars().all()
        )
        facts = list(
            (await session.execute(select(FactDO).where(FactDO.chatgroup_id == chat.chatgroup_id))).scalars().all()
        )
        assert len(topics) == 1
        assert len(facts) == 1


async def test_seed_demo_chat():
    result = await seed_demo_chat()
    assert result["message_count"] == 30
    assert result["chatgroup_id"] is not None
    assert result["chat_id"] is not None
