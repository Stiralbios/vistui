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


class EventPipeline:
    """Group contiguous messages into Events using the chat model."""

    BLOCK_SIZE = 10

    @classmethod
    async def run_round(cls, provider: Provider | None = None) -> int:
        provider = provider or Provider()
        async with get_async_sessionmaker()() as session, session.begin():
            message = await BatchQueries.get_single_message_with_state(session, "event")
            if message is None or message.embedding is None:
                return 0

            block = await BatchQueries.get_contiguous_waiting_messages(
                session,
                message.chat_id,
                "event",
                message.created_at,
                cls.BLOCK_SIZE,
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
                temperature=0.2,
            )

            groups = response.get("events", [])
            if not groups:
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
                    salience_score=0.5,
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
        lines = []
        for m in messages:
            lines.append(f"- id: {m.id} | role: {m.role} | text: {m.full_text}")
        return "\n".join(lines)


class TopicPipeline:
    """Create or update Topics for a single unprocessed message."""

    @classmethod
    async def run_round(cls, provider: Provider | None = None) -> int:
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
                temperature=0.2,
            )

            actions: list[dict] = []
            if isinstance(response, list):
                actions = response
            elif isinstance(response, dict):
                actions = response.get("actions", []) or response.get("topics", []) or []
            for action in actions:
                await cls._apply_action(session, chatgroup_id, message, action, existing_topics)

            message.processing_state = {**message.processing_state, "topic": ProcessingStatus.DONE}
            logger.info("Topic pipeline processed one message")
            return 1

    @staticmethod
    async def _chatgroup_id(session: AsyncSession, chat_id: uuid.UUID) -> uuid.UUID | None:
        chat = await session.get(ChatDO, chat_id)
        return chat.chatgroup_id if chat else None

    @staticmethod
    async def _find_similar_topics(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        embedding: list[float],
        k: int,
    ) -> list[TopicDO]:
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
        action_type = action.get("action", "skip")
        now = dt.datetime.now(dt.timezone.utc)

        if action_type == "create":
            topic = TopicDO(
                chatgroup_id=chatgroup_id,
                full_text=action.get("summary", message.full_text),
                start_at=message.created_at,
                last_edited_at=now,
                salience_score=0.5,
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
                session.add(TopicMessageLinkDO(topic_id=topic.id, message_id=message.id))


class FactPipeline:
    """Create or update Facts for a single unprocessed message."""

    @classmethod
    async def run_round(cls, provider: Provider | None = None) -> int:
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
                temperature=0.2,
            )

            actions: list[dict] = []
            if isinstance(response, list):
                actions = response
            elif isinstance(response, dict):
                actions = response.get("actions", []) or response.get("facts", []) or []
            for action in actions:
                await cls._apply_action(session, chatgroup_id, message, action, existing_facts)

            message.processing_state = {**message.processing_state, "fact": ProcessingStatus.DONE}
            logger.info("Fact pipeline processed one message")
            return 1

    @staticmethod
    async def _chatgroup_id(session: AsyncSession, chat_id: uuid.UUID) -> uuid.UUID | None:
        chat = await session.get(ChatDO, chat_id)
        return chat.chatgroup_id if chat else None

    @staticmethod
    async def _find_similar_facts(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        embedding: list[float],
        k: int,
    ) -> list[FactDO]:
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
        action_type = action.get("action", "skip")
        now = dt.datetime.now(dt.timezone.utc)

        if action_type == "create":
            fact = FactDO(
                chatgroup_id=chatgroup_id,
                full_text=action.get("summary", message.full_text),
                start_at=message.created_at,
                last_edited_at=now,
                salience_score=0.5,
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
                session.add(FactMessageLinkDO(fact_id=fact.id, message_id=message.id))
