import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from backend.batch import BatchWorker, batch_status
from backend.batch.embeddings import embed_memory_objects, embed_messages
from backend.batch.pipelines import EventPipeline, FactPipeline, TopicPipeline
from backend.batch.queries import BatchQueries
from backend.batch.salience import CHUNK_SIZE, SALIENCE_RUBRIC, SaliencePipeline, score_memory_objects
from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupConfig, ChatGroupPrompts
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO, EventMessageLinkDO
from backend.fact.models import FactDO, FactMessageLinkDO
from backend.llm import Provider
from backend.llm.provider import DecisionAnswer
from backend.message.constants import MessageRole, ProcessingStatus
from backend.message.models import MessageDO
from backend.seeders import seed_demo_chat
from backend.topic.models import TopicDO, TopicMessageLinkDO
from backend.user.models import UserDO
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import select


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


def _fake_chat_model(
    topic_content: str = '{"actions": [{"action": "create", "summary": "Demo topic"}]}',
):
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
                content = topic_content
            elif "facts" in prompt:
                content = '{"actions": [{"action": "create", "summary": "Demo fact"}]}'
            else:
                content = "{}"
            return SimpleNamespace(content=content)

    return FakeChatModel


def _fake_decide(score: float = 2.0, calls: list | None = None):
    """Return an async Provider.decide replacement that answers every question with
    the same rubric score (default 2 of 4 levels -> salience 0.5) and optionally
    records each call's arguments."""

    async def decide(self, state, questions, task=None, keep_alive=None):
        if calls is not None:
            calls.append({"state": state, "questions": questions, "task": task, "keep_alive": keep_alive})
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


async def _make_test_chat(session, protected_message_count: int = 0, use_salience: bool | None = True):
    """Create a test chatgroup + chat. The protected window defaults to 0 so test
    chats consolidate fully unless a test opts into a window explicitly."""
    user = await session.scalar(select(UserDO).where(UserDO.email == "test@example.com"))
    assert user is not None
    config = ChatGroupConfig().model_dump()
    config["protected_message_count"] = protected_message_count
    config["use_salience"] = use_salience
    chatgroup = ChatGroupDO(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Test ChatGroup",
        config=config,
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


async def _waiting_salience_state():
    """Everything done except salience: the state a message is in between embedding and
    the salience stage."""
    return {
        "embedding": ProcessingStatus.DONE,
        "salience": ProcessingStatus.WAITING,
        "event": ProcessingStatus.DONE,
        "topic": ProcessingStatus.DONE,
        "fact": ProcessingStatus.DONE,
        "last_failure_at": None,
        "retry_count": 0,
    }


async def _add_message(session, chat_id, full_text, role=MessageRole.USER, prev_id=None, state=None, created_at=None):
    """Insert one waiting message; created_at is set client-side (distinct per call)
    because the column's server default is the transaction timestamp."""
    message = MessageDO(
        id=uuid.uuid4(),
        chat_id=chat_id,
        prev_message_id=prev_id,
        full_text=full_text,
        role=role,
        embedding=None,
        created_at=created_at or dt.datetime.now(dt.timezone.utc),
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


async def test_salience_pipeline_scores_messages():
    """Messages are scored with the decision model in one decide call per chatgroup:
    rubric answers normalize to 0..1, the stage is marked done, and the questions and
    the shared state are keyed by message id."""
    calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        messages = [
            await _add_message(session, chat.id, f"Salience message {i}", state=await _waiting_salience_state())
            for i in range(3)
        ]

    with patch.object(Provider, "decide", _fake_decide(score=4.0, calls=calls)):
        handled = await SaliencePipeline.run_round()

    assert handled == 3
    assert len(calls) == 1
    call = calls[0]
    assert call["task"] == "salience"
    assert call["keep_alive"] == "10m"
    assert set(call["questions"]) == {str(message.id) for message in messages}
    question = call["questions"][str(messages[0].id)]
    assert question.criteria == SALIENCE_RUBRIC
    assert question.instructions.startswith("Assign a salience score between 0 and 1 to the message.")
    assert [entry["id"] for entry in call["state"]["messages"]] == [str(message.id) for message in messages]

    async with get_async_sessionmaker()() as session, session.begin():
        for message in messages:
            updated = await session.get(MessageDO, message.id)
            assert updated is not None
            assert updated.salience_score == 1.0  # rubric score 4 of 4 levels
            assert updated.processing_state["salience"] == ProcessingStatus.DONE


async def test_salience_pipeline_limits_and_chunks():
    """At most `limit` messages are handled per round, in decide calls of at most
    CHUNK_SIZE questions."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        for i in range(12):
            await _add_message(session, chat.id, f"Salience message {i}", state=await _waiting_salience_state())

    calls: list[dict] = []
    with patch.object(Provider, "decide", _fake_decide(calls=calls)):
        assert await SaliencePipeline.run_round(limit=10) == 10
        assert await SaliencePipeline.run_round(limit=10) == 2
    assert [len(call["questions"]) for call in calls] == [CHUNK_SIZE, CHUNK_SIZE, 2]


@pytest.mark.parametrize("use_salience", [None, False])
async def test_salience_opt_out_keeps_null(use_salience):
    """use_salience false/null opts the chatgroup out: the stage is marked done so the
    state machine progresses, but the score stays NULL and no decision call is made —
    search passes the neutral salience for those rows."""
    calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session, use_salience=use_salience)
        messages = [
            await _add_message(session, chat.id, "Opted-out message", state=await _waiting_salience_state())
            for _ in range(2)
        ]

    with patch.object(Provider, "decide", _fake_decide(calls=calls)):
        assert await SaliencePipeline.run_round() == 2
    assert calls == []

    async with get_async_sessionmaker()() as session, session.begin():
        for message in messages:
            updated = await session.get(MessageDO, message.id)
            assert updated is not None
            assert updated.salience_score is None
            assert updated.processing_state["salience"] == ProcessingStatus.DONE


async def test_salience_missing_answer_score_rolls_back():
    """A malformed decision answer (no score) fails the round: the transaction rolls
    back and the messages stay waiting for the next round."""

    async def decide(self, state, questions, task=None, keep_alive=None):
        return {name: DecisionAnswer(type="score", score=None) for name in questions}

    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        message = await _add_message(session, chat.id, "Salience message", state=await _waiting_salience_state())

    with patch.object(Provider, "decide", decide):
        with pytest.raises(ValueError, match="no score"):
            await SaliencePipeline.run_round()

    async with get_async_sessionmaker()() as session, session.begin():
        updated = await session.get(MessageDO, message.id)
        assert updated is not None
        assert updated.salience_score is None
        assert updated.processing_state["salience"] == ProcessingStatus.WAITING


async def test_score_memory_objects():
    """Events, topics and facts with a NULL salience are scored from their full_text,
    one decide call per model per chatgroup; opted-out chatgroups keep NULL and make
    no decision call."""
    calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        chatgroup_in, chat_in = await _make_test_chat(session)
        _, chat_out = await _make_test_chat(session, use_salience=None)

        event_in = EventDO(
            chat_id=chat_in.id, full_text="Event in", start_at=chat_in.created_at, end_at=chat_in.created_at
        )
        topic_in = TopicDO(
            chatgroup_id=chatgroup_in.id,
            full_text="Topic in",
            start_at=chat_in.created_at,
            last_edited_at=chat_in.created_at,
        )
        fact_in = FactDO(
            chatgroup_id=chatgroup_in.id,
            full_text="Fact in",
            start_at=chat_in.created_at,
            last_edited_at=chat_in.created_at,
        )
        event_out = EventDO(
            chat_id=chat_out.id, full_text="Event out", start_at=chat_out.created_at, end_at=chat_out.created_at
        )
        session.add_all([event_in, topic_in, fact_in, event_out])

    with patch.object(Provider, "decide", _fake_decide(score=1.0, calls=calls)):
        assert await score_memory_objects() == 3
    assert len(calls) == 3  # event + topic + fact; the opted-out chatgroup makes no call
    assert sorted(next(iter(set(call["state"]) - {"chatgroup"})) for call in calls) == ["events", "facts", "topics"]

    async with get_async_sessionmaker()() as session, session.begin():
        for obj_id, model in ((event_in.id, EventDO), (topic_in.id, TopicDO), (fact_in.id, FactDO)):
            refreshed = await session.get(model, obj_id)
            assert refreshed is not None
            assert refreshed.salience_score == 0.25  # rubric score 1 of 4 levels
        refreshed_out = await session.get(EventDO, event_out.id)
        assert refreshed_out is not None
        assert refreshed_out.salience_score is None


@pytest.mark.usefixtures("fake_langchain_client")
async def test_consolidation_requires_salience_done():
    """The event/topic/fact stages only pick up messages whose salience stage is done
    (the salience stage runs earlier in the same worker round), so a message waiting
    for salience is not consolidated yet."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        message = await _add_message(session, chat.id, "Blocked message")
        message.embedding = [0.0] * 768
        message.processing_state = {**message.processing_state, "salience": ProcessingStatus.WAITING}

    assert await EventPipeline.run_round() == 0
    assert await TopicPipeline.run_round() == 0
    assert await FactPipeline.run_round() == 0


async def test_count_waiting_includes_salience():
    """A message waiting only for salience still counts as backlog for the queue."""
    done_state = {
        "embedding": ProcessingStatus.DONE,
        "salience": ProcessingStatus.DONE,
        "event": ProcessingStatus.DONE,
        "topic": ProcessingStatus.DONE,
        "fact": ProcessingStatus.DONE,
        "last_failure_at": None,
        "retry_count": 0,
    }
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        await _add_message(
            session,
            chat.id,
            "Only salience waiting",
            state={**done_state, "salience": ProcessingStatus.WAITING},
        )
        await _add_message(session, chat.id, "Fully done", state=done_state)
        assert await BatchQueries.count_waiting_messages(session) == 1


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


async def test_topic_pipeline_single_action_response():
    """A bare single-action JSON object (no "actions" wrapper) must be applied, not dropped."""
    with (
        patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings()),
        patch(
            "backend.llm.provider.ChatOpenAI",
            _fake_chat_model(topic_content='{"action": "create", "summary": "Solo topic"}'),
        ),
    ):
        async with get_async_sessionmaker()() as session, session.begin():
            chatgroup, chat = await _make_test_chat(session)
            msg = await _add_message(session, chat.id, "Solo action message")
            msg.embedding = [0.0] * 768

        processed = await TopicPipeline.run_round()
        assert processed == 1

        async with get_async_sessionmaker()() as session, session.begin():
            topics = list(
                (await session.execute(select(TopicDO).where(TopicDO.chatgroup_id == chatgroup.id))).scalars().all()
            )
            assert len(topics) == 1
            assert topics[0].full_text == "Solo topic"
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
async def test_protected_window_skips_recent_messages():
    """The `protected_message_count` most recent messages are skipped by event/topic/fact
    consolidation, but the embedding stage still processes them."""
    async with get_async_sessionmaker()() as session, session.begin():
        chatgroup, chat = await _make_test_chat(session, protected_message_count=5)
        base = dt.datetime.now(dt.timezone.utc)
        messages = []
        for i in range(12):
            messages.append(
                await _add_message(
                    session,
                    chat.id,
                    f"Protected window message {i}",
                    created_at=base + dt.timedelta(seconds=i),
                )
            )

    # Embedding ignores the protected window: all 12 messages are embedded.
    assert await embed_messages() == 12

    # The event block only reaches the 7 messages outside the window.
    assert await EventPipeline.run_round() == 7
    assert await EventPipeline.run_round() == 0  # remaining candidates are all protected

    # Topic/fact stages process the oldest non-protected message, one per round.
    assert await TopicPipeline.run_round() == 1
    assert await FactPipeline.run_round() == 1

    async with get_async_sessionmaker()() as session, session.begin():
        first = await session.get(MessageDO, messages[0].id)
        assert first is not None
        assert first.processing_state["topic"] == ProcessingStatus.DONE
        assert first.processing_state["fact"] == ProcessingStatus.DONE

        for i, message in enumerate(messages):
            updated = await session.get(MessageDO, message.id)
            assert updated is not None
            assert updated.embedding is not None, "embedding must not respect the window"
            assert updated.processing_state["embedding"] == ProcessingStatus.DONE
            if i < 7:
                assert updated.processing_state["event"] == ProcessingStatus.DONE
            else:
                # Last 5 messages: inside the protected window, consolidation flags untouched.
                assert updated.processing_state["event"] == ProcessingStatus.WAITING
                assert updated.processing_state["topic"] == ProcessingStatus.WAITING
                assert updated.processing_state["fact"] == ProcessingStatus.WAITING


@pytest.mark.usefixtures("fake_langchain_client")
async def test_batch_worker_round():
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        msg = await _add_message(session, chat.id, "Batch round test message")
        msg.embedding = [0.0] * 768
        # A second message that only waits for salience: the round scores it via the
        # fake decision call (score 2 of 4 rubric levels -> 0.5).
        salience_msg = await _add_message(
            session,
            chat.id,
            "Salience-only message",
            state={
                "embedding": ProcessingStatus.DONE,
                "salience": ProcessingStatus.WAITING,
                "event": ProcessingStatus.DONE,
                "topic": ProcessingStatus.DONE,
                "fact": ProcessingStatus.DONE,
                "last_failure_at": None,
                "retry_count": 0,
            },
        )

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
        updated = await session.get(MessageDO, salience_msg.id)
        assert updated is not None
        assert updated.salience_score == pytest.approx(0.5)
        assert updated.processing_state["salience"] == ProcessingStatus.DONE


async def test_seed_demo_chat():
    result = await seed_demo_chat()
    assert result["message_count"] == 30
    assert result["chatgroup_id"] is not None
    assert result["chat_id"] is not None

    # Phase 4: salience is a real stage now - seeded messages wait for it, and no
    # placeholder score is written.
    async with get_async_sessionmaker()() as session, session.begin():
        messages = list(
            (await session.execute(select(MessageDO).where(MessageDO.chat_id == result["chat_id"]))).scalars().all()
        )
        assert len(messages) == 30
        for message in messages:
            assert message.processing_state["salience"] == ProcessingStatus.WAITING
            assert message.salience_score is None
