"""LLM-driven pipelines that turn embedded messages into higher-level memories.

Each pipeline's run_round processes one unit of work inside its own transaction and
returns the number of messages handled, so the BatchWorker can call them repeatedly
(and a crash never loses more than one round).
"""

import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable

from backend.batch.queries import BatchQueries
from backend.batch.status import batch_status
from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupPrompts
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO, EventMessageLinkDO
from backend.fact.models import FactDO, FactMessageLinkDO
from backend.llm import Provider
from backend.llm.prompts import FACT_CANDIDATE, TOPIC_CANDIDATE
from backend.llm.provider import ChatError, ChoiceQuestion
from backend.message.constants import MessageRole, ProcessingStatus
from backend.message.models import MessageDO
from backend.topic.models import TopicDO, TopicMessageLinkDO
from backend.utils.embeddings import embed_object, embed_text
from loguru import logger
from paradedb.sqlalchemy.vector import cosine_distance
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def record_stage_failure(message_id: uuid.UUID, state_key: str) -> None:
    """Record a consolidation failure on the message: bump retry_count and
    last_failure_at in its own transaction. The stage flag stays waiting, so later
    rounds retry the message for free."""
    async with get_async_sessionmaker()() as session, session.begin():
        message = await session.get(MessageDO, message_id)
        if message is None:
            return
        state = dict(message.processing_state)
        state["retry_count"] = state.get("retry_count", 0) + 1
        state["last_failure_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        message.processing_state = state
    batch_status.recent_failures += 1
    logger.warning(f"Stage {state_key} failed for message {message_id}")


async def _link_message_to_event(session: AsyncSession, event_id: uuid.UUID, message_id: uuid.UUID) -> None:
    """Idempotently link a message to an event."""
    await session.execute(
        insert(EventMessageLinkDO).values(event_id=event_id, message_id=message_id).on_conflict_do_nothing()
    )


async def _link_message_to_topic(session: AsyncSession, topic_id: uuid.UUID, message_id: uuid.UUID) -> None:
    """Idempotently link a message to a topic."""
    await session.execute(
        insert(TopicMessageLinkDO).values(topic_id=topic_id, message_id=message_id).on_conflict_do_nothing()
    )


async def _link_message_to_fact(session: AsyncSession, fact_id: uuid.UUID, message_id: uuid.UUID) -> None:
    """Idempotently link a message to a fact."""
    await session.execute(
        insert(FactMessageLinkDO).values(fact_id=fact_id, message_id=message_id).on_conflict_do_nothing()
    )


async def _process_waiting_messages(
    provider: Provider,
    state_key: str,
    limit: int,
    process_message: Callable[[Provider, uuid.UUID], Awaitable[bool]],
) -> int:
    """Run one consolidation stage over its waiting messages: fetch up to `limit`
    oldest outside the protected window, then process each in its own transaction. A
    message that fails keeps its waiting flag — later rounds retry it for free — with
    the failure recorded on it, and the round continues. Returns the number of messages
    handled."""
    async with get_async_sessionmaker()() as session, session.begin():
        messages = await BatchQueries.get_waiting_messages_with_state(session, state_key, limit)
    if not messages:
        return 0

    handled = 0
    for message in messages:
        try:
            if await process_message(provider, message.id):
                handled += 1
        except Exception as exc:
            logger.exception(f"Stage {state_key} failed on message {message.id}: {exc}")
            await record_stage_failure(message.id, state_key)
    if handled:
        logger.info(f"Stage {state_key} processed {handled} message(s)")
    return handled


def _participant_names(chatgroup: ChatGroupDO) -> tuple[str, str]:
    """Resolve the display names of the two participants from the chatgroup config;
    unset names fall back to the generic ones."""
    user_name = chatgroup.config.get("user_name") or "the user"
    assistant_name = chatgroup.config.get("assistant_name") or "the assistant"
    return user_name, assistant_name


def _speaker_name(message: MessageDO, user_name: str, assistant_name: str) -> str:
    """Name of the participant who wrote the message (system messages speak as the AI)."""
    return assistant_name if message.role == MessageRole.ASSISTANT else user_name


async def _chat_json_plain(provider: Provider, messages: list[dict[str, str]], task: str) -> dict | list:
    """Call the chat model without API-level JSON mode and parse the response.

    Some OpenAI-compatible proxies/quantized models echo back the JSON schema when
    `response_format={"type": "json_object"}` is used. The prompts already contain
    explicit JSON instructions, so we request plain text and parse the JSON ourselves.
    """
    raw = await provider.chat(messages, task=task, json_mode=False)
    content = raw if isinstance(raw, str) else str(raw)
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ChatError(f"Invalid JSON from chat model: {exc}") from exc


def _extract_actions(response: object, plural_key: str) -> list[dict]:
    """Normalize the LLM's JSON answer into a list of action dicts.

    Models answering in JSON-object mode produce several valid shapes: a bare array,
    an object wrapping the actions under "actions" or the pipeline-specific plural key
    ("topics"/"facts"), or a single bare action object — all must be applied.

    Also detects the failure mode where the model echoes back the JSON response schema
    instead of filling it in, and raises a ChatError so the message is retried.
    """
    if isinstance(response, list):
        actions: list[dict] = [action for action in response if isinstance(action, dict)]
        _raise_if_schema(actions)
        return actions
    if isinstance(response, dict):
        _raise_if_schema(response)
        for key in ("actions", plural_key):
            raw_actions = response.get(key)
            if isinstance(raw_actions, list):
                _raise_if_schema(raw_actions)
                return [action for action in raw_actions if isinstance(action, dict)]
        if isinstance(response.get("action"), str):
            return [response]
    return []


def _raise_if_schema(value: object) -> None:
    """Detect when the model returns a JSON Schema instead of actual actions.

    Some models echo back the schema/response_format definition (with keys like
    type/properties/required) instead of producing concrete data. Treat this as a
    parse failure so the message is retried rather than silently marked done.
    """
    if not isinstance(value, dict):
        return
    if {"type", "properties", "required"}.issubset(set(value.keys())):
        raise ChatError(f"LLM returned a JSON schema instead of data: {value}")
    items = value.get("items")
    if isinstance(items, dict) and {"type", "properties", "required"}.issubset(set(items.keys())):
        raise ChatError(f"LLM returned a JSON schema instead of data: {value}")


class EventPipeline:
    """Build a chat's events message by message: each round takes the oldest waiting
    messages and, for each, asks the decision model how it relates to the chat's last
    event — extend it, open a new one, or both conclude the last and open the next (a
    message can span the boundary). Event spans are fully flexible: the model decides
    the boundaries, one message at a time.

    The touched event's summary is rebuilt from its full member message list, so an
    extended event is never rewritten from its previous summary. Extending or
    concluding an event rewrites its full_text: like topic/fact updates, the row is
    invalidated (edited_at bumped, embedding and salience_score cleared) and re-enters
    the memory embedding and salience queues.
    """

    BOUNDARY_TASK = "event_boundary"
    SUMMARY_TASK = "event_summary"
    QUESTION = "boundary"
    # Keep the decision model loaded between the per-message calls of a round.
    KEEP_ALIVE = "10m"

    @classmethod
    async def run_round(cls, provider: Provider | None = None, limit: int = 10) -> int:
        """Process up to `limit` oldest messages waiting for the "event" stage outside
        their protected window, one message per transaction. Returns the number of
        messages handled (the shared runner records failures per message)."""
        return await _process_waiting_messages(provider or Provider(), "event", limit, cls._process_message)

    @classmethod
    async def _process_message(cls, provider: Provider, message_id: uuid.UUID) -> bool:
        """Process one waiting message in its own transaction: decide the boundary
        against the chat's last event (a chat without events opens one directly), apply
        the action, then flag the message done. Returns False when the message is no
        longer waiting (nothing to do)."""
        async with get_async_sessionmaker()() as session, session.begin():
            message = await session.get(MessageDO, message_id)
            if message is None or message.processing_state.get("event") != ProcessingStatus.WAITING:
                return False

            chatgroup = await cls._chatgroup(session, message.chat_id)
            last_event = await BatchQueries.get_last_event(session, message.chat_id)

            action = "new"
            if last_event is not None:
                action = await cls._decide_boundary(provider, chatgroup, last_event, message)

            if action == "extend":
                members = await BatchQueries.get_event_messages(session, last_event.id)
                await cls._rewrite_event(session, provider, chatgroup, last_event, members + [message])
                await _link_message_to_event(session, last_event.id, message.id)
            elif action == "new":
                await cls._create_event(session, provider, chatgroup, [message])
            elif action == "mixed":
                members = await BatchQueries.get_event_messages(session, last_event.id)
                await cls._rewrite_event(session, provider, chatgroup, last_event, members + [message])
                await _link_message_to_event(session, last_event.id, message.id)
                await cls._create_event(session, provider, chatgroup, [message])
            else:
                raise ValueError(f"unknown event boundary action: {action}")

            message.processing_state = {**message.processing_state, "event": ProcessingStatus.DONE}
            return True

    @staticmethod
    async def _chatgroup(session: AsyncSession, chat_id: uuid.UUID) -> ChatGroupDO:
        """Load the chatgroup owning the chat (its prompts drive both calls)."""
        chat = await session.get(ChatDO, chat_id)
        assert chat is not None
        chatgroup = await session.get(ChatGroupDO, chat.chatgroup_id)
        assert chatgroup is not None
        return chatgroup

    @classmethod
    async def _decide_boundary(
        cls,
        provider: Provider,
        chatgroup: ChatGroupDO,
        last_event: EventDO,
        message: MessageDO,
    ) -> str:
        """Ask the decision model how the message relates to the last event: a choice
        question over the chatgroup's boundary criteria, resolved to the action with
        the highest probability (a missing distribution is a failure, so the message
        is retried)."""
        question = ChoiceQuestion(
            instructions=chatgroup.prompts.get("event_boundary") or ChatGroupPrompts().event_boundary,
            criteria=chatgroup.prompts.get("event_boundary_criteria") or ChatGroupPrompts().event_boundary_criteria,
        )
        state = {
            "chatgroup": chatgroup.description,
            "last_event": {
                "summary": last_event.full_text,
                "start_at": last_event.start_at.isoformat(),
                "end_at": last_event.end_at.isoformat(),
            },
            "message": {
                "role": message.role,
                "text": message.full_text,
                "created_at": message.created_at.isoformat(),
            },
        }
        answers = await provider.decide(
            state,
            {cls.QUESTION: question},
            task=cls.BOUNDARY_TASK,
            keep_alive=cls.KEEP_ALIVE,
        )
        probabilities = answers[cls.QUESTION].probabilities
        if not probabilities:
            raise ValueError(f"event boundary answer has no probabilities: {answers[cls.QUESTION]}")
        return max(probabilities, key=lambda key: probabilities[key])

    @classmethod
    async def _create_event(
        cls,
        session: AsyncSession,
        provider: Provider,
        chatgroup: ChatGroupDO,
        members: list[MessageDO],
    ) -> EventDO:
        """Create a new event from its member messages: one summary call over the full
        list, the time range derived from the members, each member linked."""
        summary = await cls._summarize(provider, chatgroup, members)
        event = EventDO(
            chat_id=members[0].chat_id,
            full_text=summary,
            start_at=min(member.created_at for member in members),
            end_at=max(member.created_at for member in members),
        )
        await embed_object(event, provider)
        session.add(event)
        await session.flush()
        for member in members:
            await _link_message_to_event(session, event.id, member.id)
        return event

    @classmethod
    async def _rewrite_event(
        cls,
        session: AsyncSession,
        provider: Provider,
        chatgroup: ChatGroupDO,
        event: EventDO,
        members: list[MessageDO],
    ) -> None:
        """Rewrite an existing event from its full member list: the summary is rebuilt
        from the sources, the time range is re-derived, and the embedding is
        recomputed synchronously (salience is still refreshed by the memory-salience
        batch stage)."""
        event.full_text = await cls._summarize(provider, chatgroup, members)
        event.start_at = min(member.created_at for member in members)
        event.end_at = max(member.created_at for member in members)
        event.edited_at = dt.datetime.now(dt.timezone.utc)
        event.embedding = await embed_text(event.full_text, provider)
        event.salience_score = None

    @staticmethod
    async def _summarize(provider: Provider, chatgroup: ChatGroupDO, members: list[MessageDO]) -> str:
        """Rebuild the event summary from the full member list through the chatgroup's
        event_summary template ({messages} substituted, each message attributed to its
        speaker by the chatgroup's participant names). A response without a summary
        is a failure, so the message is retried."""
        template = chatgroup.prompts.get("event_summary") or ChatGroupPrompts().event_summary
        user_name, assistant_name = _participant_names(chatgroup)
        response = await provider.chat_json(
            [
                {
                    "role": "user",
                    "content": template.format(
                        messages=EventPipeline._format_messages(members, user_name, assistant_name)
                    ),
                }
            ],
            task=EventPipeline.SUMMARY_TASK,
        )
        summary = response.get("summary") if isinstance(response, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"event summary response has no summary: {response}")
        return summary.strip()

    @staticmethod
    def _format_messages(messages: list[MessageDO], user_name: str, assistant_name: str) -> str:
        """Render messages as a bullet list, oldest first, each attributed to the
        participant who wrote it, so the summary follows the conversation's order
        and names its speakers."""
        lines = []
        for message in messages:
            speaker = _speaker_name(message, user_name, assistant_name)
            lines.append(f"- {speaker}: {message.full_text}")
        return "\n".join(lines)


class TopicPipeline:
    """Create or update the chatgroup's Topics based on the waiting messages.

    For each message, the nearest existing topics (by embedding similarity) are offered
    to the LLM, which decides to create new topics or update/link existing ones.
    """

    @classmethod
    async def run_round(cls, provider: Provider | None = None, limit: int = 5) -> int:
        """Process up to `limit` oldest messages waiting for the "topic" stage outside
        their protected window, one message per transaction. Returns the number of
        messages handled (the shared runner records failures per message)."""
        return await _process_waiting_messages(provider or Provider(), "topic", limit, cls._process_message)

    @classmethod
    async def _process_message(cls, provider: Provider, message_id: uuid.UUID) -> bool:
        """Process one waiting message in its own transaction: ask the LLM for
        create/update actions against the nearest topics, apply them, then flag the
        message done. Returns False when the message is no longer waiting."""
        async with get_async_sessionmaker()() as session, session.begin():
            message = await session.get(MessageDO, message_id)
            if message is None or message.processing_state.get("topic") != ProcessingStatus.WAITING:
                return False

            chatgroup = await cls._chatgroup(session, message.chat_id)
            if chatgroup is None:
                return False

            existing_topics = await cls._find_similar_topics(session, chatgroup.id, message.embedding, k=5)
            topics_text = "\n".join(f"- {topic.id}: {topic.full_text}" for topic in existing_topics) or "None"
            user_name, assistant_name = _participant_names(chatgroup)
            speaker = _speaker_name(message, user_name, assistant_name)

            response = await _chat_json_plain(
                provider,
                [
                    {
                        "role": "user",
                        "content": TOPIC_CANDIDATE.format(
                            message=message.full_text,
                            speaker=speaker,
                            topics=topics_text,
                            max_topics=5,
                            user_name=user_name,
                            assistant_name=assistant_name,
                        ),
                    }
                ],
                task="topic_candidate",
            )

            actions = _extract_actions(response, "topics")
            for action in actions:
                await cls._apply_action(session, chatgroup.id, message, action, existing_topics, provider)

            message.processing_state = {**message.processing_state, "topic": ProcessingStatus.DONE}
            return True

    @staticmethod
    async def _chatgroup(session: AsyncSession, chat_id: uuid.UUID) -> ChatGroupDO | None:
        """Resolve the chatgroup a chat belongs to (topics are scoped per chatgroup;
        its config carries the participant names)."""
        chat = await session.get(ChatDO, chat_id)
        return await session.get(ChatGroupDO, chat.chatgroup_id) if chat else None

    @staticmethod
    async def _find_similar_topics(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        embedding: list[float] | None,
        k: int,
    ) -> list[TopicDO]:
        """Return the chatgroup's k topics closest (cosine) to the message embedding,
        or none when the message is not embedded yet (the LLM then has no candidates
        to update)."""
        if embedding is None:
            return []
        stmt = (
            select(TopicDO)
            .where(TopicDO.chatgroup_id == chatgroup_id)
            .where(TopicDO.embedding.isnot(None))
            .order_by(cosine_distance(TopicDO.embedding, embedding))
            .limit(k)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def _apply_action(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        message: MessageDO,
        action: dict,
        existing_topics: list[TopicDO],
        provider: Provider,
    ) -> None:
        """Apply one LLM-decided action: create a new topic, or update an existing one and
        link the message to it. The topic embedding is recomputed synchronously so the
        candidate pool is never stale."""
        action_type = action.get("action", "skip")
        now = dt.datetime.now(dt.timezone.utc)

        if action_type == "create":
            topic = TopicDO(
                chatgroup_id=chatgroup_id,
                full_text=action.get("summary", message.full_text),
                start_at=message.created_at,
            )
            await embed_object(topic, provider)
            session.add(topic)
            await session.flush()
            await _link_message_to_topic(session, topic.id, message.id)
            return

        topic_id = action.get("topic_id")
        if action_type == "update" and topic_id:
            topic = await session.get(TopicDO, uuid.UUID(topic_id))
            if topic is None:
                topic = next((t for t in existing_topics if str(t.id) == topic_id), None)
            if topic is not None:
                topic.full_text = action.get("summary", topic.full_text)
                topic.edited_at = now
                topic.embedding = await embed_text(topic.full_text, provider)
                topic.salience_score = None
                await _link_message_to_topic(session, topic.id, message.id)


class FactPipeline:
    """Create or update the chatgroup's Facts based on the waiting messages.

    For each message, the nearest existing facts (by embedding similarity) are offered
    to the LLM, which decides to create new facts or update/link existing ones.

    Like
    events and topics, facts skip messages inside the protected window: a protected
    message is never added to a fact until it ages out of the window. A fact itself is
    never protected from updates — once the updating message is outside the window, the
    fact can be rewritten in place (with `embedding` and `salience_score` cleared for
    regeneration).
    """

    @classmethod
    async def run_round(cls, provider: Provider | None = None, limit: int = 5) -> int:
        """Process up to `limit` oldest messages waiting for the "fact" stage outside
        their protected window, one message per transaction. Returns the number of
        messages handled (the shared runner records failures per message)."""
        return await _process_waiting_messages(provider or Provider(), "fact", limit, cls._process_message)

    @classmethod
    async def _process_message(cls, provider: Provider, message_id: uuid.UUID) -> bool:
        """Process one waiting message in its own transaction: ask the LLM for
        create/update actions against the nearest facts, apply them, then flag the
        message done. Returns False when the message is no longer waiting."""
        async with get_async_sessionmaker()() as session, session.begin():
            message = await session.get(MessageDO, message_id)
            if message is None or message.processing_state.get("fact") != ProcessingStatus.WAITING:
                return False

            chatgroup = await cls._chatgroup(session, message.chat_id)
            if chatgroup is None:
                return False

            existing_facts = await cls._find_similar_facts(session, chatgroup.id, message.embedding, k=10)
            facts_text = "\n".join(f"- {fact.id}: {fact.full_text}" for fact in existing_facts) or "None"
            user_name, assistant_name = _participant_names(chatgroup)
            speaker = _speaker_name(message, user_name, assistant_name)

            response = await _chat_json_plain(
                provider,
                [
                    {
                        "role": "user",
                        "content": FACT_CANDIDATE.format(
                            message=message.full_text,
                            speaker=speaker,
                            facts=facts_text,
                            max_facts=10,
                            user_name=user_name,
                            assistant_name=assistant_name,
                        ),
                    }
                ],
                task="fact_candidate",
            )

            actions = _extract_actions(response, "facts")
            for action in actions:
                await cls._apply_action(session, chatgroup.id, message, action, existing_facts, provider)

            message.processing_state = {**message.processing_state, "fact": ProcessingStatus.DONE}
            return True

    @staticmethod
    async def _chatgroup(session: AsyncSession, chat_id: uuid.UUID) -> ChatGroupDO | None:
        """Resolve the chatgroup a chat belongs to (facts are scoped per chatgroup;
        its config carries the participant names)."""
        chat = await session.get(ChatDO, chat_id)
        return await session.get(ChatGroupDO, chat.chatgroup_id) if chat else None

    @staticmethod
    async def _find_similar_facts(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        embedding: list[float] | None,
        k: int,
    ) -> list[FactDO]:
        """Return the chatgroup's k facts closest (cosine) to the message embedding,
        or none when the message is not embedded yet (the LLM then has no candidates
        to update)."""
        if embedding is None:
            return []
        stmt = (
            select(FactDO)
            .where(FactDO.chatgroup_id == chatgroup_id)
            .where(FactDO.embedding.isnot(None))
            .order_by(cosine_distance(FactDO.embedding, embedding))
            .limit(k)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def _apply_action(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        message: MessageDO,
        action: dict,
        existing_facts: list[FactDO],
        provider: Provider,
    ) -> None:
        """Apply one LLM-decided action: create a new fact, or update an existing one and
        link the message to it. The fact embedding is recomputed synchronously so the
        candidate pool is never stale."""
        action_type = action.get("action", "skip")
        now = dt.datetime.now(dt.timezone.utc)

        if action_type == "create":
            fact = FactDO(
                chatgroup_id=chatgroup_id,
                full_text=action.get("summary", message.full_text),
                start_at=message.created_at,
            )
            await embed_object(fact, provider)
            session.add(fact)
            await session.flush()
            await _link_message_to_fact(session, fact.id, message.id)
            return

        fact_id = action.get("fact_id")
        if action_type == "update" and fact_id:
            fact = await session.get(FactDO, uuid.UUID(fact_id))
            if fact is None:
                fact = next((f for f in existing_facts if str(f.id) == fact_id), None)
            if fact is not None:
                fact.full_text = action.get("summary", fact.full_text)
                fact.edited_at = now
                fact.embedding = await embed_text(fact.full_text, provider)
                fact.salience_score = None
                await _link_message_to_fact(session, fact.id, message.id)
