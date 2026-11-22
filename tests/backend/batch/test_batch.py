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
from backend.chatgroup.schemas import ChatGroupConfig, ChatGroupPrompts
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO, EventMessageLinkDO
from backend.fact.models import FactDO, FactMessageLinkDO
from backend.llm import Provider
from backend.llm.prompts import DEFAULT_EVENT_BOUNDARY_CRITERIA, DEFAULT_SALIENCE_RUBRIC
from backend.llm.provider import ChoiceQuestion, DecisionAnswer
from backend.message.constants import MessageRole, ProcessingStatus
from backend.message.models import MessageDO
from backend.seeders import seed_demo_chat
from backend.settings import AppSettings
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
    event_contents: list[str] | None = None,
    calls: list[dict] | None = None,
):
    """Return a LangChain ChatOpenAI whose ainvoke returns deterministic JSON per
    task, detected from the prompt content (the candidate prompts' fact_id/topic_id
    action keys, event summary prompt), optionally recording each call. Event answers
    are consumed from `event_contents` in order, one per summary call."""

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
                content = topic_content
            elif "event" in lowered:
                content = (event_contents or ['{"summary": "Demo event summary"}']).pop(0)
            else:
                content = "{}"
            if calls is not None:
                calls.append({"prompt": prompt, "content": content})
            return SimpleNamespace(content=content)

    return FakeChatModel


def _fake_decide(score: float = 2.0, choice: str = "new", calls: list | None = None):
    """Return an async Provider.decide replacement that answers every question with
    the same rubric score (score questions, default 2 of 4 levels -> salience 0.5) or
    the same action (choice questions: probability 1.0 on the action, 0.0 elsewhere),
    optionally recording each call's arguments."""

    async def decide(self, state, questions, task=None, keep_alive=None):
        if calls is not None:
            calls.append({"state": state, "questions": questions, "task": task, "keep_alive": keep_alive})
        answers = {}
        for name, question in questions.items():
            if isinstance(question, ChoiceQuestion):
                probabilities = {key: 0.0 for key in question.criteria}
                probabilities[choice] = 1.0
                answers[name] = DecisionAnswer(type="choice", choice=choice, probabilities=probabilities)
            else:
                answers[name] = DecisionAnswer(type="score", score=score)
        return answers

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
    session,
    protected_message_count: int = 0,
    use_salience: bool = True,
    prompts: dict | None = None,
    config: dict | None = None,
):
    """Create a test chatgroup + chat. The protected window defaults to 0 so test
    chats consolidate fully unless a test opts into a window explicitly; `prompts`
    overrides individual prompt keys and `config` individual config keys on top of
    the defaults."""
    user = await session.scalar(select(UserDO).where(UserDO.email == "test@example.com"))
    assert user is not None
    config_data = ChatGroupConfig().model_dump()
    config_data["protected_message_count"] = protected_message_count
    config_data["use_salience"] = use_salience
    if config:
        config_data.update(config)
    prompts_data = ChatGroupPrompts().model_dump()
    if prompts:
        prompts_data.update(prompts)
    chatgroup = ChatGroupDO(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Test ChatGroup",
        config=config_data,
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


async def test_consolidation_round_limit_settings():
    """The consolidation stages share the per-round message-limit pattern of the
    batch stages: each round processes up to its limit of the oldest waiting messages."""
    settings = AppSettings()
    assert settings.VISTUI_BATCH_EVENT_MESSAGE_LIMIT == 10
    assert settings.VISTUI_BATCH_TOPIC_MESSAGE_LIMIT == 5
    assert settings.VISTUI_BATCH_FACT_MESSAGE_LIMIT == 5


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
async def test_event_pipeline_creates_first_event_without_decision():
    """A chat without events opens one directly: no boundary decision call, the
    summary built from the single message, the time range collapsed onto it, the
    message linked and flagged done."""
    decide_calls: list[dict] = []
    chat_calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        message = await _add_message(session, chat.id, "First event message")

    with (
        patch.object(Provider, "decide", _fake_decide(calls=decide_calls)),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model(calls=chat_calls)),
    ):
        assert await EventPipeline.run_round() == 1

    assert decide_calls == []  # no last event: the boundary is forced
    assert len(chat_calls) == 1
    assert "First event message" in chat_calls[0]["prompt"]

    async with get_async_sessionmaker()() as session, session.begin():
        events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().all())
        assert len(events) == 1
        assert events[0].full_text == "Demo event summary"
        assert events[0].start_at == message.created_at
        assert events[0].end_at == message.created_at
        assert events[0].edited_at is not None
        links = list(
            (await session.execute(select(EventMessageLinkDO).where(EventMessageLinkDO.event_id == events[0].id)))
            .scalars()
            .all()
        )
        assert [link.message_id for link in links] == [message.id]
        updated = await session.get(MessageDO, message.id)
        assert updated is not None
        assert updated.processing_state["event"] == ProcessingStatus.DONE


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_pipeline_boundary_decision_payload():
    """With a last event present, the boundary goes to the decision model: the
    chatgroup's event_boundary instructions and criteria reach the choice question
    verbatim, the task and keep-alive are the boundary ones, and the state carries the
    chatgroup description, the last event and the message."""
    decide_calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        chatgroup, chat = await _make_test_chat(
            session,
            prompts={
                "event_boundary": "Relate the message to the last event.",
                "event_boundary_criteria": {"extend": "Continues.", "new": "Starts.", "mixed": "Spans."},
            },
        )
        first = await _add_message(session, chat.id, "First event message")
        second = await _add_message(session, chat.id, "Second event message", prev_id=first.id)

    with patch.object(Provider, "decide", _fake_decide(choice="new", calls=decide_calls)):
        assert await EventPipeline.run_round() == 2  # forced new, then decided new

    assert len(decide_calls) == 1  # only the second message needed a boundary decision
    call = decide_calls[0]
    assert call["task"] == "event_boundary"
    assert call["keep_alive"] == "10m"
    question = call["questions"]["boundary"]
    assert isinstance(question, ChoiceQuestion)
    assert question.instructions == "Relate the message to the last event."
    assert question.criteria == {"extend": "Continues.", "new": "Starts.", "mixed": "Spans."}
    assert call["state"]["chatgroup"] == chatgroup.description
    assert call["state"]["last_event"]["summary"] == "Demo event summary"
    assert call["state"]["last_event"]["start_at"] == first.created_at.isoformat()
    assert call["state"]["last_event"]["end_at"] == first.created_at.isoformat()
    assert call["state"]["message"] == {
        "role": second.role,
        "text": second.full_text,
        "created_at": second.created_at.isoformat(),
    }


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_pipeline_new_action_leaves_last_event():
    """Decision "new": the last event is left exactly as it was (no rewrite, no
    invalidation, no new link), and a fresh event is created from the message alone."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        first = await _add_message(session, chat.id, "First event message")

    with patch.object(Provider, "decide", _fake_decide(choice="new")):
        assert await EventPipeline.run_round() == 1

    async with get_async_sessionmaker()() as session, session.begin():
        second = await _add_message(
            session,
            chat.id,
            "Second event message",
            prev_id=first.id,
            created_at=first.created_at + dt.timedelta(minutes=5),
        )
        event = (await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().one()
        event.embedding = [0.0] * 768
        event.salience_score = 0.5

    with patch.object(Provider, "decide", _fake_decide(choice="new")):
        assert await EventPipeline.run_round() == 1

    async with get_async_sessionmaker()() as session, session.begin():
        events = list(
            (await session.execute(select(EventDO).where(EventDO.chat_id == chat.id).order_by(EventDO.start_at.asc())))
            .scalars()
            .all()
        )
        assert len(events) == 2
        first_event, second_event = events
        assert first_event.full_text == "Demo event summary"  # untouched
        assert first_event.embedding == [0.0] * 768
        assert first_event.salience_score == 0.5
        assert second_event.start_at == second.created_at
        assert second_event.end_at == second.created_at
        first_links = list(
            (await session.execute(select(EventMessageLinkDO).where(EventMessageLinkDO.event_id == first_event.id)))
            .scalars()
            .all()
        )
        second_links = list(
            (await session.execute(select(EventMessageLinkDO).where(EventMessageLinkDO.event_id == second_event.id)))
            .scalars()
            .all()
        )
        assert [link.message_id for link in first_links] == [first.id]
        assert [link.message_id for link in second_links] == [second.id]


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_pipeline_extend_action_rewrites_from_members():
    """Decision "extend": the event's summary is rebuilt from all its member messages
    plus the new one, the time range grows to cover it, and the row is invalidated like
    a topic/fact update (edited_at bumped, embedding and salience cleared for the
    memory queues) with the new message linked."""
    chat_calls: list[dict] = []
    old_edited_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session, prompts={"event_summary": "Event so far:\n{messages}"})
        first = await _add_message(session, chat.id, "Hiking plan message")

    with patch.object(Provider, "decide", _fake_decide(choice="new")):
        assert await EventPipeline.run_round() == 1

    async with get_async_sessionmaker()() as session, session.begin():
        second = await _add_message(
            session,
            chat.id,
            "Trail choice message",
            prev_id=first.id,
            created_at=first.created_at + dt.timedelta(minutes=5),
        )
        event = (await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().one()
        event.embedding = [0.0] * 768
        event.salience_score = 0.5
        event.edited_at = old_edited_at

    with (
        patch.object(Provider, "decide", _fake_decide(choice="extend")),
        patch(
            "backend.llm.provider.ChatOpenAI",
            _fake_chat_model(event_contents=['{"summary": "Extended summary"}'], calls=chat_calls),
        ),
    ):
        assert await EventPipeline.run_round() == 1

    assert len(chat_calls) == 1
    assert chat_calls[0]["prompt"].startswith("Event so far:")
    assert "Hiking plan message" in chat_calls[0]["prompt"]  # rebuilt from all member texts
    assert "Trail choice message" in chat_calls[0]["prompt"]

    async with get_async_sessionmaker()() as session, session.begin():
        events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().all())
        assert len(events) == 1
        assert events[0].full_text == "Extended summary"
        assert events[0].end_at == second.created_at
        assert events[0].edited_at > old_edited_at
        assert events[0].embedding is None  # requeued for the memory stages
        assert events[0].salience_score is None
        links = list(
            (await session.execute(select(EventMessageLinkDO).where(EventMessageLinkDO.event_id == events[0].id)))
            .scalars()
            .all()
        )
        assert {link.message_id for link in links} == {first.id, second.id}
        updated = await session.get(MessageDO, second.id)
        assert updated is not None
        assert updated.processing_state["event"] == ProcessingStatus.DONE


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_pipeline_mixed_action_spans_boundary():
    """Decision "mixed": the boundary message concludes the last event and opens the
    next — both events are re-summarized with it among their sources, and it is linked
    to both."""
    chat_calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        first = await _add_message(session, chat.id, "Hiking plan message")

    with patch.object(Provider, "decide", _fake_decide(choice="new")):
        assert await EventPipeline.run_round() == 1

    async with get_async_sessionmaker()() as session, session.begin():
        second = await _add_message(
            session,
            chat.id,
            "Trail choice message",
            prev_id=first.id,
            created_at=first.created_at + dt.timedelta(minutes=5),
        )

    with (
        patch.object(Provider, "decide", _fake_decide(choice="mixed")),
        patch(
            "backend.llm.provider.ChatOpenAI",
            _fake_chat_model(
                event_contents=['{"summary": "Close summary"}', '{"summary": "Open summary"}'],
                calls=chat_calls,
            ),
        ),
    ):
        assert await EventPipeline.run_round() == 1

    assert len(chat_calls) == 2
    assert "Hiking plan message" in chat_calls[0]["prompt"]
    assert "Trail choice message" in chat_calls[0]["prompt"]  # the close includes the boundary message
    assert "Trail choice message" in chat_calls[1]["prompt"]
    assert "Hiking plan message" not in chat_calls[1]["prompt"]

    async with get_async_sessionmaker()() as session, session.begin():
        events = list(
            (await session.execute(select(EventDO).where(EventDO.chat_id == chat.id).order_by(EventDO.start_at.asc())))
            .scalars()
            .all()
        )
        assert len(events) == 2
        first_event, second_event = events
        assert first_event.full_text == "Close summary"
        assert first_event.end_at == second.created_at
        assert first_event.embedding is None  # the rewrite requeues the row
        assert first_event.salience_score is None
        assert second_event.full_text == "Open summary"
        assert second_event.start_at == second.created_at
        assert second_event.end_at == second.created_at
        first_links = list(
            (await session.execute(select(EventMessageLinkDO).where(EventMessageLinkDO.event_id == first_event.id)))
            .scalars()
            .all()
        )
        second_links = list(
            (await session.execute(select(EventMessageLinkDO).where(EventMessageLinkDO.event_id == second_event.id)))
            .scalars()
            .all()
        )
        assert {link.message_id for link in first_links} == {first.id, second.id}  # linked to both
        assert [link.message_id for link in second_links] == [second.id]
        updated = await session.get(MessageDO, second.id)
        assert updated is not None
        assert updated.processing_state["event"] == ProcessingStatus.DONE


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_pipeline_respects_limit():
    """Up to `limit` messages are processed per round, oldest first; the boundary
    question offers the default criteria when the chatgroup keeps them."""
    decide_calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        for i in range(12):
            await _add_message(session, chat.id, f"Event message {i}")

    with patch.object(Provider, "decide", _fake_decide(choice="new", calls=decide_calls)):
        assert await EventPipeline.run_round(limit=10) == 10
        assert await EventPipeline.run_round(limit=10) == 2

    assert len(decide_calls) == 11  # the first message is a forced new, not a decision
    assert decide_calls[0]["questions"]["boundary"].criteria == DEFAULT_EVENT_BOUNDARY_CRITERIA

    async with get_async_sessionmaker()() as session, session.begin():
        events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().all())
        assert len(events) == 12  # one event per decided-new message


async def test_event_pipeline_failure_isolated_per_message():
    """A message whose boundary decision fails keeps its waiting flag — later rounds
    retry it for free — with the failure recorded on it (retry_count, last_failure_at),
    and the round continues with the remaining messages."""
    inner = _fake_decide(choice="new")

    async def decide(self, state, questions, task=None, keep_alive=None):
        if state["message"]["text"] == "Fails once":
            raise RuntimeError("decision model down")
        return await inner(self, state, questions, task=task, keep_alive=keep_alive)

    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        ok_first = await _add_message(session, chat.id, "Ok one")
        failing = await _add_message(session, chat.id, "Fails once", prev_id=ok_first.id)
        ok_second = await _add_message(session, chat.id, "Ok two", prev_id=failing.id)

    failures_before = batch_status.recent_failures
    with (
        patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings()),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model()),
        patch.object(Provider, "decide", decide),
    ):
        assert await EventPipeline.run_round() == 2  # ok_first and ok_second; the failure is skipped
    assert batch_status.recent_failures == failures_before + 1

    async with get_async_sessionmaker()() as session, session.begin():
        for message in (ok_first, ok_second):
            updated = await session.get(MessageDO, message.id)
            assert updated is not None
            assert updated.processing_state["event"] == ProcessingStatus.DONE
        failed = await session.get(MessageDO, failing.id)
        assert failed is not None
        assert failed.processing_state["event"] == ProcessingStatus.WAITING  # retried later
        assert failed.processing_state["retry_count"] == 1
        assert failed.processing_state["last_failure_at"] is not None


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_boundary_answer_without_probabilities_fails():
    """A choice answer without a probability distribution cannot be resolved: the
    message keeps its waiting flag with the failure recorded on it, and the last
    event is left untouched."""

    async def decide(self, state, questions, task=None, keep_alive=None):
        return {name: DecisionAnswer(type="choice", choice="new") for name in questions}

    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        first = await _add_message(session, chat.id, "First event message")

    assert await EventPipeline.run_round() == 1  # first message: forced new (fixture decide)

    async with get_async_sessionmaker()() as session, session.begin():
        second = await _add_message(session, chat.id, "Second event message", prev_id=first.id)

    with patch.object(Provider, "decide", decide):
        assert await EventPipeline.run_round() == 0

    async with get_async_sessionmaker()() as session, session.begin():
        updated = await session.get(MessageDO, second.id)
        assert updated is not None
        assert updated.processing_state["event"] == ProcessingStatus.WAITING
        assert updated.processing_state["retry_count"] == 1
        assert updated.processing_state["last_failure_at"] is not None
        events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().all())
        assert len(events) == 1  # the garbage answer opened nothing


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_summary_response_without_summary_fails():
    """A summary response without a summary key cannot be applied: the message keeps
    its waiting flag with the failure recorded on it, and no event is created."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        message = await _add_message(session, chat.id, "First event message")

    with patch(
        "backend.llm.provider.ChatOpenAI",
        _fake_chat_model(event_contents=["{}"]),
    ):
        assert await EventPipeline.run_round() == 0

    async with get_async_sessionmaker()() as session, session.begin():
        updated = await session.get(MessageDO, message.id)
        assert updated is not None
        assert updated.processing_state["event"] == ProcessingStatus.WAITING
        assert updated.processing_state["retry_count"] == 1
        events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat.id))).scalars().all())
        assert events == []


@pytest.mark.usefixtures("fake_langchain_client")
async def test_topic_pipeline_attributes_speaker_and_participant_names():
    """The candidate prompt tells the model who is who and who is speaking: the
    chatgroup's participant names introduce both participants, and the new message is
    attributed to the participant who wrote it."""
    chat_calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session, config={"user_name": "Aurélien", "assistant_name": "Glas"})
        await _add_message(session, chat.id, "I used to ride horses", role=MessageRole.ASSISTANT)

    with patch("backend.llm.provider.ChatOpenAI", _fake_chat_model(calls=chat_calls)):
        assert await TopicPipeline.run_round() == 1

    prompt = " ".join(chat_calls[0]["prompt"].split())
    assert "Aurélien (the human)" in prompt
    assert "Glas (the AI)" in prompt
    assert "New message from Glas:" in prompt
    assert "I used to ride horses" in prompt


@pytest.mark.usefixtures("fake_langchain_client")
async def test_fact_pipeline_attributes_speaker_and_participant_names():
    """Same attribution contract on the fact side, with a message from the human."""
    chat_calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session, config={"user_name": "Aurélien", "assistant_name": "Glas"})
        await _add_message(session, chat.id, "My mom has cancer")

    with patch("backend.llm.provider.ChatOpenAI", _fake_chat_model(calls=chat_calls)):
        assert await FactPipeline.run_round() == 1

    prompt = " ".join(chat_calls[0]["prompt"].split())
    assert "Aurélien (the human)" in prompt
    assert "Glas (the AI)" in prompt
    assert "New message from Aurélien:" in prompt


@pytest.mark.usefixtures("fake_langchain_client")
async def test_candidate_prompts_fall_back_to_generic_participant_names():
    """Chatgroups without configured participant names get the generic ones: the
    prompt still names the message's speaker, so statements stay attributed."""
    chat_calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        await _add_message(session, chat.id, "Generic names message")

    with patch("backend.llm.provider.ChatOpenAI", _fake_chat_model(calls=chat_calls)):
        assert await TopicPipeline.run_round() == 1

    prompt = " ".join(chat_calls[0]["prompt"].split())
    assert "the user (the human)" in prompt
    assert "the assistant (the AI)" in prompt
    assert "New message from the user:" in prompt


@pytest.mark.usefixtures("fake_langchain_client")
async def test_event_summary_renders_participant_names():
    """The summary's message list names each message's speaker using the chatgroup's
    participant names, so the rebuilt summaries attribute statements correctly."""
    chat_calls: list[dict] = []
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session, config={"user_name": "Aurélien", "assistant_name": "Glas"})
        human = await _add_message(session, chat.id, "Human speaks")
        await _add_message(session, chat.id, "AI speaks", role=MessageRole.ASSISTANT, prev_id=human.id)

    with (
        patch.object(Provider, "decide", _fake_decide(choice="new")),
        patch("backend.llm.provider.ChatOpenAI", _fake_chat_model(calls=chat_calls)),
    ):
        assert await EventPipeline.run_round() == 2

    assert len(chat_calls) == 2
    assert "- Aurélien: Human speaks" in chat_calls[0]["prompt"]
    assert "- Glas: AI speaks" in chat_calls[1]["prompt"]


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
async def test_topic_pipeline_respects_limit():
    """Up to `limit` messages are processed per round, oldest first."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        for i in range(7):
            await _add_message(session, chat.id, f"Topic limit message {i}")

    assert await TopicPipeline.run_round(limit=5) == 5
    assert await TopicPipeline.run_round(limit=5) == 2

    async with get_async_sessionmaker()() as session, session.begin():
        topics = list(
            (await session.execute(select(TopicDO).where(TopicDO.chatgroup_id == chat.chatgroup_id))).scalars().all()
        )
        assert len(topics) == 7  # the fake model creates one topic per message


@pytest.mark.usefixtures("fake_langchain_client")
async def test_fact_pipeline_respects_limit():
    """Up to `limit` messages are processed per round, oldest first."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        for i in range(7):
            await _add_message(session, chat.id, f"Fact limit message {i}")

    assert await FactPipeline.run_round(limit=5) == 5
    assert await FactPipeline.run_round(limit=5) == 2

    async with get_async_sessionmaker()() as session, session.begin():
        facts = list(
            (await session.execute(select(FactDO).where(FactDO.chatgroup_id == chat.chatgroup_id))).scalars().all()
        )
        assert len(facts) == 7  # the fake model creates one fact per message


def _unparseable_json_model(fail_text: str):
    """Return a chat model whose answer is unparseable JSON for prompts mentioning
    `fail_text`, a valid action otherwise."""

    class UnparseableModel(ChatOpenAI):
        def __init__(self, **kwargs):
            pass

        def bind(self, **kwargs):
            return self

        async def ainvoke(self, input: Any, config: Any = None, *, stop: Any = None, **kwargs: Any) -> Any:
            prompt = " ".join(getattr(m, "content", str(m)) for m in input)
            content = "not json" if fail_text in prompt else '{"actions": [{"action": "create", "summary": "Ok"}]}'
            return SimpleNamespace(content=content)

    return UnparseableModel


@pytest.mark.usefixtures("fake_langchain_client")
async def test_topic_pipeline_failure_isolated_per_message():
    """A message whose candidate call fails keeps its waiting flag — later rounds retry
    it for free — with the failure recorded on it, and the round continues with the
    remaining messages."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        ok_first = await _add_message(session, chat.id, "Topic ok one")
        failing = await _add_message(session, chat.id, "Topic fails once", prev_id=ok_first.id)
        ok_second = await _add_message(session, chat.id, "Topic ok two", prev_id=failing.id)

    failures_before = batch_status.recent_failures
    with patch("backend.llm.provider.ChatOpenAI", _unparseable_json_model("Topic fails once")):
        assert await TopicPipeline.run_round() == 2  # ok_first and ok_second; the failure is skipped
    assert batch_status.recent_failures == failures_before + 1

    async with get_async_sessionmaker()() as session, session.begin():
        for message in (ok_first, ok_second):
            updated = await session.get(MessageDO, message.id)
            assert updated is not None
            assert updated.processing_state["topic"] == ProcessingStatus.DONE
        failed = await session.get(MessageDO, failing.id)
        assert failed is not None
        assert failed.processing_state["topic"] == ProcessingStatus.WAITING  # retried later
        assert failed.processing_state["retry_count"] == 1
        assert failed.processing_state["last_failure_at"] is not None


@pytest.mark.usefixtures("fake_langchain_client")
async def test_fact_pipeline_failure_isolated_per_message():
    """A message whose candidate call fails keeps its waiting flag — later rounds retry
    it for free — with the failure recorded on it, and the round continues with the
    remaining messages."""
    async with get_async_sessionmaker()() as session, session.begin():
        _, chat = await _make_test_chat(session)
        ok_first = await _add_message(session, chat.id, "Fact ok one")
        failing = await _add_message(session, chat.id, "Fact fails once", prev_id=ok_first.id)
        ok_second = await _add_message(session, chat.id, "Fact ok two", prev_id=failing.id)

    failures_before = batch_status.recent_failures
    with patch("backend.llm.provider.ChatOpenAI", _unparseable_json_model("Fact fails once")):
        assert await FactPipeline.run_round() == 2  # ok_first and ok_second; the failure is skipped
    assert batch_status.recent_failures == failures_before + 1

    async with get_async_sessionmaker()() as session, session.begin():
        for message in (ok_first, ok_second):
            updated = await session.get(MessageDO, message.id)
            assert updated is not None
            assert updated.processing_state["fact"] == ProcessingStatus.DONE
        failed = await session.get(MessageDO, failing.id)
        assert failed is not None
        assert failed.processing_state["fact"] == ProcessingStatus.WAITING  # retried later
        assert failed.processing_state["retry_count"] == 1
        assert failed.processing_state["last_failure_at"] is not None


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

    # The event stage processes the 7 messages outside the window (its per-round limit
    # is 10), the topic and fact stages the 5 oldest of them (limit 5).
    assert await EventPipeline.run_round() == 7
    assert await EventPipeline.run_round() == 0  # remaining candidates are all protected

    # Topic/fact stages process up to five oldest non-protected messages per round.
    assert await TopicPipeline.run_round() == 5
    assert await FactPipeline.run_round() == 5

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
