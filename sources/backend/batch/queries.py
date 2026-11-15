"""Reusable DB queries for the batch worker.

They center on MessageDO.processing_state, a JSONB dict mapping each pipeline stage
("embedding", "event", "topic", "fact") to a ProcessingStatus value.

Consolidation queries (event/topic/fact) only select messages outside their chat's
protected window — the `protected_message_count` most recent messages, configured on
the chat's chatgroup. The embedding stage ignores the window: every message is
embedded as soon as possible.
"""

import datetime as dt
import uuid
from typing import Any, Protocol, TypeVar

from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupConfig
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased


class _HasEmbedding(Protocol):
    embedding: Any
    created_at: Any


T = TypeVar("T", bound=_HasEmbedding)

# Fallback when a chatgroup's config predates the key; matches the ChatGroupConfig default.
_DEFAULT_PROTECTED_COUNT = ChatGroupConfig().protected_message_count


def _is_outside_protected_window() -> ColumnElement[bool]:
    """Build a SQL predicate keeping only messages outside their chat's protected window.

    A message is inside the window when fewer than the chatgroup's
    `protected_message_count` setting messages in the same chat are newer; ties on
    created_at are broken by id so messages sharing a timestamp are still ordered.
    """
    newer = aliased(MessageDO)
    newer_count = (
        select(func.count())
        .select_from(newer)
        .where(newer.chat_id == MessageDO.chat_id)
        .where(
            or_(
                newer.created_at > MessageDO.created_at,
                and_(newer.created_at == MessageDO.created_at, newer.id > MessageDO.id),
            )
        )
        .correlate(MessageDO)
        .scalar_subquery()
    )
    protected_count = (
        select(func.coalesce(ChatGroupDO.config["protected_message_count"].as_integer(), _DEFAULT_PROTECTED_COUNT))
        .select_from(ChatGroupDO)
        .where(ChatDO.id == MessageDO.chat_id)
        .where(ChatGroupDO.id == ChatDO.chatgroup_id)
        .correlate(MessageDO)
        .scalar_subquery()
    )
    return newer_count >= protected_count


class BatchQueries:
    """Database queries used by the batch consolidation worker."""

    @staticmethod
    async def get_messages_with_state(
        session: AsyncSession,
        state_key: str,
        limit: int,
    ) -> list[MessageDO]:
        """Return the oldest messages whose processing_state[state_key] is waiting.

        The protected window is ignored: the embedding stage uses this and must process
        every message, recent or not.
        """
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
        """Return the oldest single message waiting for the given pipeline, skipping
        messages inside their chat's protected window."""
        stmt = (
            select(MessageDO)
            .where(MessageDO.processing_state[state_key].astext == ProcessingStatus.WAITING)
            .where(_is_outside_protected_window())
            .order_by(MessageDO.created_at.asc(), MessageDO.id.asc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def get_contiguous_waiting_messages(
        session: AsyncSession,
        chat_id: uuid.UUID,
        state_key: str,
        min_created_at: dt.datetime,
        block_size: int,
    ) -> list[MessageDO]:
        """Return up to block_size messages in a chat waiting for the pipeline, starting
        at or after a timestamp. Messages inside their chat's protected window are
        excluded, so the block may be shorter than block_size."""
        stmt = (
            select(MessageDO)
            .where(MessageDO.chat_id == chat_id)
            .where(MessageDO.processing_state[state_key].astext == ProcessingStatus.WAITING)
            .where(MessageDO.embedding.isnot(None))
            .where(MessageDO.created_at >= min_created_at)
            .where(_is_outside_protected_window())
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
        """Count messages that still have at least one waiting pipeline flag.

        Messages inside the protected window are included: they stay counted until
        their chat grows enough to make them eligible for consolidation.
        """
        stmt = (
            select(func.count())
            .select_from(MessageDO)
            .where(
                or_(
                    MessageDO.processing_state["embedding"].astext == ProcessingStatus.WAITING,
                    MessageDO.processing_state["event"].astext == ProcessingStatus.WAITING,
                    MessageDO.processing_state["topic"].astext == ProcessingStatus.WAITING,
                    MessageDO.processing_state["fact"].astext == ProcessingStatus.WAITING,
                )
            )
        )
        res = await session.execute(stmt)
        return res.scalar_one()
