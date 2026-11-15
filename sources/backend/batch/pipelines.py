"""LLM-driven pipelines that turn embedded messages into higher-level memories.

Each pipeline's run_round processes one unit of work inside its own transaction and
returns the number of messages handled, so the BatchWorker can call them repeatedly
(and a crash never loses more than one round).
"""

import datetime as dt
import uuid

from backend.batch.queries import BatchQueries
from backend.chat.models import ChatDO
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO, EventMessageLinkDO
from backend.fact.models import FactDO, FactMessageLinkDO
from backend.llm import Provider
from backend.llm.prompts import EVENT_GROUPING, FACT_CANDIDATE, TOPIC_CANDIDATE
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from backend.topic.models import TopicDO, TopicMessageLinkDO
from loguru import logger
from paradedb.sqlalchemy.vector import cosine_distance
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _extract_actions(response: object, plural_key: str) -> list[dict]:
    """Normalize the LLM's JSON answer into a list of action dicts.

    Models answering in JSON-object mode produce several valid shapes: a bare array,
    an object wrapping the actions under "actions" or the pipeline-specific plural key
    ("topics"/"facts"), or a single bare action object — all must be applied.
    """
    if isinstance(response, list):
        return [action for action in response if isinstance(action, dict)]
    if isinstance(response, dict):
        for key in ("actions", plural_key):
            actions = response.get(key)
            if isinstance(actions, list):
                return [action for action in actions if isinstance(action, dict)]
        if isinstance(response.get("action"), str):
            return [response]
    return []


class EventPipeline:
    """Group contiguous messages of a chat into Events using the chat model.

    Events are the coarsest memory unit: a block of consecutive messages summarized
    together, with links back to each source message.
    """

    BLOCK_SIZE = 10

    @classmethod
    async def run_round(cls, provider: Provider | None = None, block_size: int | None = None) -> int:
        """Process one block: take the oldest message waiting for the "event" stage
        outside its chat's protected window and, in a single LLM call, group it with up to
        `block_size` (default BLOCK_SIZE) contiguous embedded, non-protected messages of
        the same chat into events. Returns the number of messages processed, 0 if
        nothing to do."""
        provider = provider or Provider()
        block_size = block_size if block_size is not None else cls.BLOCK_SIZE
        async with get_async_sessionmaker()() as session, session.begin():
            message = await BatchQueries.get_single_message_with_state(session, "event")
            if message is None or message.embedding is None:
                return 0

            block = await BatchQueries.get_contiguous_waiting_messages(
                session,
                message.chat_id,
                "event",
                message.created_at,
                block_size,
            )
            if not block:
                return 0

            messages_prompt = cls._format_messages(block)
            response = await provider.chat_json(
                [
                    {
                        "role": "user",
                        "content": EVENT_GROUPING.format(messages=messages_prompt),
                    }
                ],
                task="event",
            )

            groups = response.get("events", [])
            if not groups:
                # LLM returned no grouping: fall back to one event covering the whole block.
                groups = [{"summary": "General conversation block", "message_ids": [str(m.id) for m in block]}]

            for group in groups:
                summary = group.get("summary", "")
                ids = {uuid.UUID(mid) for mid in group.get("message_ids", [])}
                linked = [m for m in block if m.id in ids]
                if not linked:
                    linked = block

                start_at = min(m.created_at for m in linked)
                end_at = max(m.created_at for m in linked)
                event = EventDO(
                    chat_id=message.chat_id,
                    full_text=summary,
                    start_at=start_at,
                    end_at=end_at,
                )
                session.add(event)
                await session.flush()
                for m in linked:
                    session.add(EventMessageLinkDO(event_id=event.id, message_id=m.id))

            for m in block:
                m.processing_state = {**m.processing_state, "event": ProcessingStatus.DONE}

            logger.info(f"Event pipeline processed {len(block)} messages")
            return len(block)

    @staticmethod
    def _format_messages(messages: list[MessageDO]) -> str:
        """Render messages as a bullet list so the LLM can reference them by id."""
        lines = []
        for m in messages:
            lines.append(f"- id: {m.id} | role: {m.role} | text: {m.full_text}")
        return "\n".join(lines)


class TopicPipeline:
    """Create or update the chatgroup's Topics based on a single waiting message.

    For each message, the nearest existing topics (by embedding similarity) are offered
    to the LLM, which decides to create new topics or update/link existing ones.
    """

    @classmethod
    async def run_round(cls, provider: Provider | None = None) -> int:
        """Process the oldest message waiting for the "topic" stage outside its chat's
        protected window: ask the LLM for create/update actions against the nearest topics,
        then apply them. Returns 1 if a message was processed, else 0 (nothing waiting,
        not yet embedded, or protected)."""
        provider = provider or Provider()
        async with get_async_sessionmaker()() as session, session.begin():
            message = await BatchQueries.get_single_message_with_state(session, "topic")
            if message is None or message.embedding is None:
                return 0

            chatgroup_id = await cls._chatgroup_id(session, message.chat_id)
            if chatgroup_id is None:
                return 0

            existing_topics = await cls._find_similar_topics(session, chatgroup_id, message.embedding, k=5)
            topics_text = "\n".join(f"- {topic.id}: {topic.full_text}" for topic in existing_topics) or "None"

            response = await provider.chat_json(
                [
                    {
                        "role": "user",
                        "content": TOPIC_CANDIDATE.format(
                            message=message.full_text,
                            topics=topics_text,
                            max_topics=5,
                        ),
                    }
                ],
                task="topic_candidate",
            )

            actions = _extract_actions(response, "topics")
            for action in actions:
                await cls._apply_action(session, chatgroup_id, message, action, existing_topics)

            message.processing_state = {**message.processing_state, "topic": ProcessingStatus.DONE}
            logger.info("Topic pipeline processed one message")
            return 1

    @staticmethod
    async def _chatgroup_id(session: AsyncSession, chat_id: uuid.UUID) -> uuid.UUID | None:
        """Resolve the chatgroup a chat belongs to (topics are scoped per chatgroup)."""
        chat = await session.get(ChatDO, chat_id)
        return chat.chatgroup_id if chat else None

    @staticmethod
    async def _find_similar_topics(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        embedding: list[float],
        k: int,
    ) -> list[TopicDO]:
        """Return the chatgroup's k topics closest (cosine) to the message embedding."""
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
    ) -> None:
        """Apply one LLM-decided action: create a new topic, or update an existing one and
        link the message to it (updated topics get their embedding cleared, so they are
        re-embedded on the next embedding round)."""
        action_type = action.get("action", "skip")
        now = dt.datetime.now(dt.timezone.utc)

        if action_type == "create":
            topic = TopicDO(
                chatgroup_id=chatgroup_id,
                full_text=action.get("summary", message.full_text),
                start_at=message.created_at,
                last_edited_at=now,
            )
            session.add(topic)
            await session.flush()
            session.add(TopicMessageLinkDO(topic_id=topic.id, message_id=message.id))
            return

        topic_id = action.get("topic_id")
        if action_type == "update" and topic_id:
            topic = await session.get(TopicDO, uuid.UUID(topic_id))
            if topic is None:
                topic = next((t for t in existing_topics if str(t.id) == topic_id), None)
            if topic is not None:
                topic.full_text = action.get("summary", topic.full_text)
                topic.last_edited_at = now
                topic.embedding = None
                topic.salience_score = None
                session.add(TopicMessageLinkDO(topic_id=topic.id, message_id=message.id))


class FactPipeline:
    """Create or update the chatgroup's Facts based on a single waiting message.

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
    async def run_round(cls, provider: Provider | None = None) -> int:
        """Process the oldest message waiting for the "fact" stage outside its chat's
        protected window: ask the LLM for create/update actions against the nearest facts,
        then apply them. Returns 1 if a message was processed, else 0 (nothing waiting,
        not yet embedded, or protected)."""
        provider = provider or Provider()
        async with get_async_sessionmaker()() as session, session.begin():
            message = await BatchQueries.get_single_message_with_state(session, "fact")
            if message is None or message.embedding is None:
                return 0

            chatgroup_id = await cls._chatgroup_id(session, message.chat_id)
            if chatgroup_id is None:
                return 0

            existing_facts = await cls._find_similar_facts(session, chatgroup_id, message.embedding, k=5)
            facts_text = "\n".join(f"- {fact.id}: {fact.full_text}" for fact in existing_facts) or "None"

            response = await provider.chat_json(
                [
                    {
                        "role": "user",
                        "content": FACT_CANDIDATE.format(
                            message=message.full_text,
                            facts=facts_text,
                            max_facts=5,
                        ),
                    }
                ],
                task="fact_candidate",
            )

            actions = _extract_actions(response, "facts")
            for action in actions:
                await cls._apply_action(session, chatgroup_id, message, action, existing_facts)

            message.processing_state = {**message.processing_state, "fact": ProcessingStatus.DONE}
            logger.info("Fact pipeline processed one message")
            return 1

    @staticmethod
    async def _chatgroup_id(session: AsyncSession, chat_id: uuid.UUID) -> uuid.UUID | None:
        """Resolve the chatgroup a chat belongs to (facts are scoped per chatgroup)."""
        chat = await session.get(ChatDO, chat_id)
        return chat.chatgroup_id if chat else None

    @staticmethod
    async def _find_similar_facts(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        embedding: list[float],
        k: int,
    ) -> list[FactDO]:
        """Return the chatgroup's k facts closest (cosine) to the message embedding."""
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
    ) -> None:
        """Apply one LLM-decided action: create a new fact, or update an existing one and
        link the message to it (updated facts get their embedding cleared, so they are
        re-embedded on the next embedding round)."""
        action_type = action.get("action", "skip")
        now = dt.datetime.now(dt.timezone.utc)

        if action_type == "create":
            fact = FactDO(
                chatgroup_id=chatgroup_id,
                full_text=action.get("summary", message.full_text),
                start_at=message.created_at,
                last_edited_at=now,
            )
            session.add(fact)
            await session.flush()
            session.add(FactMessageLinkDO(fact_id=fact.id, message_id=message.id))
            return

        fact_id = action.get("fact_id")
        if action_type == "update" and fact_id:
            fact = await session.get(FactDO, uuid.UUID(fact_id))
            if fact is None:
                fact = next((f for f in existing_facts if str(f.id) == fact_id), None)
            if fact is not None:
                fact.full_text = action.get("summary", fact.full_text)
                fact.last_edited_at = now
                fact.embedding = None
                fact.salience_score = None
                session.add(FactMessageLinkDO(fact_id=fact.id, message_id=message.id))
