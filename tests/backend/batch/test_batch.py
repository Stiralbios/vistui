import datetime as dt
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from backend.batch import BatchWorker, batch_status
from backend.batch.embeddings import embed_memory_objects, embed_messages
from backend.batch.pipelines import EventPipeline, FactPipeline, TopicPipeline
from backend.batch.queries import BatchQueries
from backend.batch.salience import SaliencePipeline, score_memory_objects
from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import DEFAULT_SALIENCE_RUBRIC, ChatGroupConfig, ChatGroupPrompts
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


async def _make_test_chat(
    session, protected_message_count: int = 0, use_salience: bool = True, prompts: dict | None = None
):
    """Create a test chatgroup + chat. The protected window defaults to 0 so test
    chats consolidate fully unless a test opts into a window explicitly; `prompts`
    overrides individual prompt keys on top of the defaults."""
    user = await session.scalar(select(UserDO).where(UserDO.email == "test@example.com"))
    assert user is not None
    config = ChatGroupConfig().model_dump()
    config["protected_message_count"] = protected_message_count
    config["use_salience"] = use_salience
    prompts_data = ChatGroupPrompts().model_dump()
    if prompts:
        prompts_data.update(prompts)
    chatgroup = ChatGroupDO(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Test ChatGroup",
        config=config,
        prompts=prompts_data,
        ranking_formula="default",
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


async def _done_state():
    """Consolidation flags all done: a message whose only remaining work would be the
    column-queued stages (embedding NULL, or embedded with a NULL salience)."""
    return {
        "event": ProcessingStatus.DONE,
        "topic": ProcessingStatus.DONE,
        "fact": ProcessingStatus.DONE,
        "last_failure_at": None,
        "retry_count": 0,
    }


async def _add_message(
    session, chat_id, full_text, role=MessageRole.USER, prev_id=None, state=None, created_at=None, embedding=None
):
    """Insert one waiting message; created_at/edited_at are set client-side (distinct
    per call) because the column's server default is the transaction timestamp."""
    written_at = created_at or dt.datetime.now(dt.timezone.utc)
    message = MessageDO(
        id=uuid.uuid4(),
        chat_id=chat_id,
        prev_message_id=prev_id,
        full_text=full_text,
        role=role,
        embedding=embedding,
        salience_score=None,
        created_at=written_at,
        edited_at=written_at,
        processing_state=state
        or {
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
        assert "embedding" not in updated.processing_state


async def test_salience_pipeline_scores_messages():
    """Each unscored message is scored in its own decide() call, element per element:
    the state carries the chatgroup description plus the single message, one score
    question against the chatgroup's default rubric, answers normalized to 0..1 by the
    rubric length. The salience stage does not wait for the embedding stage — only the
    NULL salience_score column marks the queue."""
    calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        messages = [await _add_message(session, chat.id, f"Salience message {i}") for i in range(3)]

    with patch.object(Provider, "decide", _fake_decide(score=4.0, calls=calls)):
        handled = await SaliencePipeline.run_round()

    assert handled == 3
    assert len(calls) == 3
    for call, message in zip(calls, messages, strict=True):
        assert call["task"] == "salience"
        assert call["keep_alive"] == "10m"
        assert set(call["questions"]) == {"salience"}
        question = call["questions"]["salience"]
        assert question.criteria == DEFAULT_SALIENCE_RUBRIC
        assert question.instructions == (
            "Judge how much the message in the state is worth remembering for future conversations."
        )
        assert call["state"]["message"] == {"role": message.role, "text": message.full_text}
        assert "chatgroup" in call["state"]

    async with get_async_sessionmaker()() as session, session.begin():
        for message in messages:
            updated = await session.get(MessageDO, message.id)
            assert updated is not None
            assert updated.salience_score == 1.0  # rubric score 4 of 4 levels
            assert "salience" not in updated.processing_state


async def test_salience_pipeline_custom_prompt_and_rubric():
    """A chatgroup can fully shape the salience decision payload: its instruction
    template reaches the question verbatim (with {kind} substituted — stray braces
    stay literal), its rubric becomes the question criteria, and the normalization
    adapts to the rubric length (2 levels: the top answer maps to 1.0)."""
    calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(
            session,
            prompts={
                "salience": "Rank the {kind}: {score} braces stay literal.",
                "salience_rubric": ["Forgettable.", "Essential."],
            },
        )
        message = await _add_message(session, chat.id, "Custom rubric message")

    with patch.object(Provider, "decide", _fake_decide(score=1.0, calls=calls)):
        assert await SaliencePipeline.run_round() == 1

    assert len(calls) == 1
    question = calls[0]["questions"]["salience"]
    assert question.criteria == ["Forgettable.", "Essential."]
    assert question.instructions == "Rank the message: {score} braces stay literal."

    async with get_async_sessionmaker()() as session, session.begin():
        updated = await session.get(MessageDO, message.id)
        assert updated is not None
        # Top answer of a 2-level rubric -> score 1 of max 1 -> salience 1.0.
        assert updated.salience_score == 1.0


async def test_salience_pipeline_respects_limit():
    """At most `limit` messages are scored per round, one decide() call per message."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        for i in range(12):
            await _add_message(session, chat.id, f"Salience message {i}")

    calls: list[dict] = []
    with patch.object(Provider, "decide", _fake_decide(calls=calls)):
        assert await SaliencePipeline.run_round(limit=10) == 10
        assert await SaliencePipeline.run_round(limit=10) == 2
    assert len(calls) == 12  # one call per scored message
    assert all(len(call["questions"]) == 1 for call in calls)


@pytest.mark.parametrize("use_salience", [False])
async def test_salience_opt_out_excluded_from_queue(use_salience):
    """use_salience false opts the chatgroup out: the selection query excludes
    their messages entirely (the NULL score is permanent, so without the filter they
    would clog the queue forever) — no decide call, nothing scored, score stays NULL."""
    calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session, use_salience=use_salience)
        messages = [await _add_message(session, chat.id, "Opted-out message") for _ in range(2)]

    with patch.object(Provider, "decide", _fake_decide(calls=calls)):
        assert await SaliencePipeline.run_round() == 0
    assert calls == []

    async with get_async_sessionmaker()() as session, session.begin():
        for message in messages:
            updated = await session.get(MessageDO, message.id)
            assert updated is not None
            assert updated.salience_score is None


async def test_salience_missing_answer_score_rolls_back():
    """A malformed decision answer (no score) fails the round: the transaction rolls
    back, the salience_score column stays NULL, so the message is retried for free on
    the next round (the NULL column is the queue marker)."""

    async def decide(self, state, questions, task=None, keep_alive=None):
        return {name: DecisionAnswer(type="score", score=None) for name in questions}

    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        message = await _add_message(session, chat.id, "Salience message")

    with patch.object(Provider, "decide", decide):
        with pytest.raises(ValueError, match="no score"):
            await SaliencePipeline.run_round()

    async with get_async_sessionmaker()() as session, session.begin():
        updated = await session.get(MessageDO, message.id)
        assert updated is not None
        assert updated.salience_score is None  # still queued: retried next round


async def test_score_memory_objects():
    """Events, topics and facts with a NULL salience are scored from their full_text,
    one decide() call per object; opted-out chatgroups keep NULL and make no decision
    call."""
    calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        chatgroup_in, chat_in = await _make_test_chat(session)
        _, chat_out = await _make_test_chat(session, use_salience=False)

        event_in = EventDO(
            chat_id=chat_in.id, full_text="Event in", start_at=chat_in.created_at, end_at=chat_in.created_at
        )
        topic_in = TopicDO(
            chatgroup_id=chatgroup_in.id,
            full_text="Topic in",
            start_at=chat_in.created_at,
            edited_at=chat_in.created_at,
        )
        fact_in = FactDO(
            chatgroup_id=chatgroup_in.id,
            full_text="Fact in",
            start_at=chat_in.created_at,
            edited_at=chat_in.created_at,
        )
        event_out = EventDO(
            chat_id=chat_out.id, full_text="Event out", start_at=chat_out.created_at, end_at=chat_out.created_at
        )
        session.add_all([event_in, topic_in, fact_in, event_out])

    with patch.object(Provider, "decide", _fake_decide(score=1.0, calls=calls)):
        assert await score_memory_objects() == 3
    assert len(calls) == 3  # one call per object; the opted-out chatgroup makes no call
    assert sorted(next(iter(set(call["state"]) - {"chatgroup"})) for call in calls) == ["event", "fact", "topic"]
    assert all(set(call["questions"]) == {"salience"} for call in calls)

    async with get_async_sessionmaker()() as session, session.begin():
        for obj_id, model in ((event_in.id, EventDO), (topic_in.id, TopicDO), (fact_in.id, FactDO)):
            refreshed = await session.get(model, obj_id)
            assert refreshed is not None
            assert refreshed.salience_score == 0.25  # rubric score 1 of 4 levels
        refreshed_out = await session.get(EventDO, event_out.id)
        assert refreshed_out is not None
        assert refreshed_out.salience_score is None


@pytest.mark.usefixtures("fake_langchain_client")
async def test_consolidation_ignores_embedding_and_salience():
    """Consolidation only waits for the protected window: embedding and salience are
    computed by their own stages and are not prerequisites. A message with neither an
    embedding nor a salience score is picked up right away, and the salience_score
    column keeps its NULL value."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        await _add_message(session, chat.id, "Not embedded, not scored")

    assert await EventPipeline.run_round() == 1
    assert await TopicPipeline.run_round() == 1
    assert await FactPipeline.run_round() == 1

    async with get_async_sessionmaker()() as session, session.begin():
        updated = await session.scalar(select(MessageDO).where(MessageDO.chat_id == chat.id))
        assert updated is not None
        assert updated.embedding is None
        assert updated.salience_score is None
        assert updated.processing_state["event"] == ProcessingStatus.DONE
        assert updated.processing_state["topic"] == ProcessingStatus.DONE
        assert updated.processing_state["fact"] == ProcessingStatus.DONE


async def test_count_waiting_messages():
    """The queue depth derives from the same column conditions the stages select on:
    a NULL embedding or a NULL salience score counts (opted-in chatgroups only for
    salience), and a waiting consolidation flag counts."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        _, chat_out = await _make_test_chat(session, use_salience=False)
        # 1. waiting for embedding and salience (counts)
        await _add_message(session, chat.id, "Not embedded")
        # 2. embedded, waiting for salience (counts: chatgroup opted in)
        await _add_message(session, chat.id, "Embedded unscored", embedding=[0.0] * 768)
        # 3. embedded + scored, consolidation done (does not count)
        scored = await _add_message(session, chat.id, "All done", embedding=[0.0] * 768, state=await _done_state())
        scored.salience_score = 0.5
        # 4. embedded + NULL salience in an opted-out chatgroup, consolidation done
        #    (does not count: the NULL score is permanent, not backlog)
        await _add_message(session, chat_out.id, "Opted out unscored", embedding=[0.0] * 768, state=await _done_state())
        # 5. scored but waiting for the event consolidation (counts via the flag)
        waiting_event = await _add_message(
            session, chat.id, "Waiting event", embedding=[0.0] * 768, state=await _done_state()
        )
        waiting_event.salience_score = 0.5
        waiting_event.processing_state = {**waiting_event.processing_state, "event": ProcessingStatus.WAITING}

        assert await BatchQueries.count_waiting_messages(session) == 3


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
            messages.append(msg)
            prev = msg.id

    processed = await EventPipeline.run_round()
    assert processed == 3

    async with get_async_sessionmaker()() as session, session.begin():
        events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().all())
        assert len(events) == 1
        assert events[0].full_text == "Demo event"
        # Creating the event wrote its text: edited_at is set.
        assert events[0].edited_at is not None
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
        await _add_message(session, chat.id, "I love hiking in the mountains")

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
            await _add_message(session, chat.id, "Solo action message")

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
async def test_topic_rewrite_bumps_edited_at():
    """An LLM rewrite of an existing topic is a content edit: the pipeline bumps the
    topic's edited_at (and requeues its derived data)."""
    old_edited_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    with (
        patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings()),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model()),
    ):
        async with get_async_sessionmaker()() as session, session.begin():
            chatgroup, chat = await _make_test_chat(session)
            msg = await _add_message(session, chat.id, "Topic update message")
            topic = TopicDO(
                id=uuid.uuid4(),
                chatgroup_id=chatgroup.id,
                full_text="Old summary",
                start_at=msg.created_at,
                edited_at=old_edited_at,
                embedding=[0.0] * 768,
            )
            session.add(topic)

        with patch(
            "backend.llm.provider.ChatOpenAI",
            _fake_chat_model(
                topic_content=json.dumps(
                    {"actions": [{"action": "update", "topic_id": str(topic.id), "summary": "New summary"}]}
                )
            ),
        ):
            assert await TopicPipeline.run_round() == 1

        async with get_async_sessionmaker()() as session, session.begin():
            refreshed = await session.get(TopicDO, topic.id)
            assert refreshed is not None
            assert refreshed.full_text == "New summary"
            assert refreshed.edited_at is not None
            assert refreshed.edited_at > old_edited_at
            # The rewrite requeues the topic's derived data.
            assert refreshed.embedding is None
            assert refreshed.salience_score is None


@pytest.mark.usefixtures("fake_langchain_client")
async def test_fact_pipeline():
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        await _add_message(session, chat.id, "My favorite color is blue")

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
    """The `protected_message_count` most recent messages are never added to
    facts/events/topics (consolidation skips them until newer messages push them out of
    the window), but the embedding stage still processes them. The chatgroup opts out of
    salience here so the test drives the consolidation pipelines directly."""
    async with get_async_sessionmaker()() as session, session.begin():
        chatgroup, chat = await _make_test_chat(session, protected_message_count=5, use_salience=False)
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
            if i < 7:
                assert updated.processing_state["event"] == ProcessingStatus.DONE
            else:
                # Last 5 messages: inside the protected window, never consolidated.
                assert updated.processing_state["event"] == ProcessingStatus.WAITING
                assert updated.processing_state["topic"] == ProcessingStatus.WAITING
                assert updated.processing_state["fact"] == ProcessingStatus.WAITING


@pytest.mark.usefixtures("fake_langchain_client")
async def test_batch_worker_round():
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        msg = await _add_message(session, chat.id, "Batch round test message")
        # A second message whose only remaining work is the salience scoring: the round
        # scores it via the fake decision call (score 2 of 4 rubric levels -> 0.5).
        salience_msg = await _add_message(
            session,
            chat.id,
            "Salience-only message",
            state=await _done_state(),
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
        # The first message was scored in the same round before consolidation.
        first = await session.get(MessageDO, msg.id)
        assert first is not None
        assert first.salience_score == pytest.approx(0.5)
        assert first.processing_state["event"] == ProcessingStatus.DONE
        # Batch stages derive data, they never rewrite content: edited_at stays at its
        # creation value.
        assert first.edited_at == first.created_at
        assert updated.edited_at == updated.created_at


@pytest.mark.usefixtures("fake_langchain_client")
async def test_worker_round_respects_salience_limit(monkeypatch):
    """VISTUI_BATCH_SALIENCE_LIMIT caps how many messages a worker round scores (the
    other VISTUI_BATCH_* limits wire into their stages the same way)."""
    monkeypatch.setenv("VISTUI_BATCH_SALIENCE_LIMIT", "2")
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        for i in range(3):
            await _add_message(
                session,
                chat.id,
                f"Salience limit message {i}",
                state=await _done_state(),
            )

    await BatchWorker().run_round()

    async with get_async_sessionmaker()() as session, session.begin():
        scored = list(
            (
                await session.execute(
                    select(MessageDO).where(MessageDO.chat_id == chat.id, MessageDO.salience_score.isnot(None))
                )
            )
            .scalars()
            .all()
        )
        assert len(scored) == 2


async def test_seed_demo_chat():
    result = await seed_demo_chat()
    assert result["message_count"] == 30
    assert result["chatgroup_id"] is not None
    assert result["chat_id"] is not None

    # The columns are the queue markers: seeded messages start with a NULL embedding
    # and a NULL salience, and the state carries no keys for them.
    async with get_async_sessionmaker()() as session, session.begin():
        messages = list(
            (await session.execute(select(MessageDO).where(MessageDO.chat_id == result["chat_id"]))).scalars().all()
        )
        assert len(messages) == 30
        for message in messages:
            assert "embedding" not in message.processing_state
            assert "salience" not in message.processing_state
            assert message.embedding is None
            assert message.salience_score is None
