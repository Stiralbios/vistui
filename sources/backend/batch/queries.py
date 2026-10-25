import datetime as dt
import uuid
from typing import Any, Protocol, TypeVar

from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession


class _HasEmbedding(Protocol):
    embedding: Any
    created_at: Any


T = TypeVar("T", bound=_HasEmbedding)


class BatchQueries:
    """Database queries used by the batch consolidation worker."""

    @staticmethod
    async def get_messages_with_state(
        session: AsyncSession,
        state_key: str,
        limit: int,
    ) -> list[MessageDO]:
        """Return the oldest messages whose processing_state[state_key] is waiting."""
        stmt = (
            select(MessageDO)
            .where(MessageDO.processing_state[state_key].astext == ProcessingStatus.WAITING)
            .order_by(MessageDO.created_at.asc(), MessageDO.id.asc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_single_message_with_state(
        session: AsyncSession,
        state_key: str,
    ) -> MessageDO | None:
        """Return the oldest single message waiting for the given pipeline."""
        rows = await BatchQueries.get_messages_with_state(session, state_key, 1)
        return rows[0] if rows else None

    @staticmethod
    async def get_contiguous_waiting_messages(
        session: AsyncSession,
        chat_id: uuid.UUID,
        state_key: str,
        min_created_at: dt.datetime,
        block_size: int,
    ) -> list[MessageDO]:
        """Return up to block_size messages in a chat waiting for the pipeline,
        starting at or after a timestamp."""
        stmt = (
            select(MessageDO)
            .where(MessageDO.chat_id == chat_id)
            .where(MessageDO.processing_state[state_key].astext == ProcessingStatus.WAITING)
            .where(MessageDO.embedding.isnot(None))
            .where(MessageDO.created_at >= min_created_at)
            .order_by(MessageDO.created_at.asc(), MessageDO.id.asc())
            .limit(block_size)
        )

        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_memory_objects_without_embedding(
        session: AsyncSession,
        model: type[T],
        limit: int,
    ) -> list[T]:
        """Return objects of the given model whose embedding column is NULL."""
        stmt = select(model).where(model.embedding.is_(None)).order_by(model.created_at.asc()).limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def count_waiting_messages(session: AsyncSession) -> int:
        """Count messages that still have at least one waiting pipeline flag."""
        stmt = select(MessageDO).where(
            or_(
                MessageDO.processing_state["embedding"].astext == ProcessingStatus.WAITING,
                MessageDO.processing_state["event"].astext == ProcessingStatus.WAITING,
                MessageDO.processing_state["topic"].astext == ProcessingStatus.WAITING,
                MessageDO.processing_state["fact"].astext == ProcessingStatus.WAITING,
            )
        )
        res = await session.execute(stmt)
        return len(list(res.scalars().all()))
