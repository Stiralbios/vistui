"""Reusable DB queries for the batch worker.

The embedding and salience stages are driven by the message columns themselves,
exactly like the memory-object stages: `embedding IS NULL` marks a message waiting for
the embedding stage, and `salience_score IS NULL` marks one waiting for the salience
stage. A failed round rolls back, the column stays NULL, and the next round retries
for free. The `processing_state` JSONB only carries the consolidation flags ("event",
"topic", "fact") plus the retry metadata (`retry_count`, `last_failure_at`).

Consolidation queries (event/topic/fact) only select messages outside their chat's
protected window — the `protected_message_count` most recent messages, configured on
the chat's chatgroup. Embedding and salience are not prerequisites: they are computed
by their own stages, and the embedding and salience stages ignore the window: every
message is embedded and scored as soon as possible.
"""

import uuid
from typing import Any, Protocol, TypeVar

from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupConfig
from backend.event.models import EventDO, EventMessageLinkDO
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from sqlalchemy import Boolean, ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased


class _HasEmbedding(Protocol):
    embedding: Any
    created_at: Any


class _HasSalience(Protocol):
    salience_score: Any
    created_at: Any


T = TypeVar("T", bound=_HasEmbedding)
S = TypeVar("S", bound=_HasSalience)


def _is_outside_protected_window() -> ColumnElement[bool]:
    """Build a SQL predicate keeping only messages outside their chat's protected window.

    A message is inside the window when fewer than the chatgroup's
    `protected_message_count` setting messages in the same chat are newer; ties on
    created_at are broken by id so messages sharing a timestamp are still ordered.
    Protected messages are never added to facts, events or topics.
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
        select(
            func.coalesce(
                ChatGroupDO.config["protected_message_count"].as_integer(),
                ChatGroupConfig().protected_message_count,
            )
        )
        .select_from(ChatGroupDO)
        .where(ChatDO.id == MessageDO.chat_id)
        .where(ChatGroupDO.id == ChatDO.chatgroup_id)
        .correlate(MessageDO)
        .scalar_subquery()
    )
    return newer_count >= protected_count


def _use_salience_optin() -> ColumnElement[bool]:
    """Build a SQL predicate keeping only messages of chatgroups opted into salience
    scoring: the `use_salience` config key is a plain boolean, missing meaning the
    default true."""
    return func.coalesce(ChatGroupDO.config["use_salience"].astext.cast(Boolean), True)


class BatchQueries:
    """Database queries used by the batch consolidation worker."""

    @staticmethod
    async def get_messages_without_embedding(
        session: AsyncSession,
        limit: int,
    ) -> list[MessageDO]:
        """Return the oldest messages whose embedding column is NULL (the embedding
        stage's queue marker). The protected window is ignored: every message is
        embedded, recent or not."""
        stmt = (
            select(MessageDO)
            .where(MessageDO.embedding.is_(None))
            .order_by(MessageDO.created_at.asc(), MessageDO.id.asc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_messages_waiting_for_salience(
        session: AsyncSession,
        limit: int,
    ) -> list[MessageDO]:
        """Return the oldest messages whose salience_score column is NULL (the salience
        stage's queue marker), restricted to chatgroups opted into salience scoring —
        opted-out chatgroups keep the NULL score by design, so without the SQL filter
        they would clog the queue forever."""
        stmt = (
            select(MessageDO)
            .join(ChatDO, ChatDO.id == MessageDO.chat_id)
            .join(ChatGroupDO, ChatGroupDO.id == ChatDO.chatgroup_id)
            .where(MessageDO.salience_score.is_(None))
            .where(_use_salience_optin())
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
        messages inside their chat's protected window: protected messages are never
        added to facts, events or topics."""
        stmt = (
            select(MessageDO)
            .join(ChatDO, ChatDO.id == MessageDO.chat_id)
            .join(ChatGroupDO, ChatGroupDO.id == ChatDO.chatgroup_id)
            .where(MessageDO.processing_state[state_key].astext == ProcessingStatus.WAITING)
            .where(_is_outside_protected_window())
            .order_by(MessageDO.created_at.asc(), MessageDO.id.asc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def get_waiting_messages_with_state(
        session: AsyncSession,
        state_key: str,
        limit: int,
    ) -> list[MessageDO]:
        """Return up to `limit` oldest messages waiting for the given pipeline, skipping
        protected messages — the plural form of get_single_message_with_state, for the
        consolidation pipelines that process several messages per round."""
        stmt = (
            select(MessageDO)
            .join(ChatDO, ChatDO.id == MessageDO.chat_id)
            .join(ChatGroupDO, ChatGroupDO.id == ChatDO.chatgroup_id)
            .where(MessageDO.processing_state[state_key].astext == ProcessingStatus.WAITING)
            .where(_is_outside_protected_window())
            .order_by(MessageDO.created_at.asc(), MessageDO.id.asc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_last_event(session: AsyncSession, chat_id: uuid.UUID) -> EventDO | None:
        """Return the chat's most recent event (greatest end_at, id tiebreak): the
        streaming event pipeline extends or concludes it for each new message."""
        stmt = (
            select(EventDO)
            .where(EventDO.chat_id == chat_id)
            .order_by(EventDO.end_at.desc(), EventDO.id.desc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def get_event_messages(session: AsyncSession, event_id: uuid.UUID) -> list[MessageDO]:
        """Return an event's member messages oldest-first: the event summary is rebuilt
        from them, never from the previous summary."""
        stmt = (
            select(MessageDO)
            .join(EventMessageLinkDO, EventMessageLinkDO.message_id == MessageDO.id)
            .where(EventMessageLinkDO.event_id == event_id)
            .order_by(MessageDO.created_at.asc(), MessageDO.id.asc())
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
    async def get_memory_objects_without_salience(
        session: AsyncSession,
        model: type[S],
        limit: int,
    ) -> list[S]:
        """Return objects of the given model whose salience_score column is NULL."""
        stmt = select(model).where(model.salience_score.is_(None)).order_by(model.created_at.asc()).limit(limit)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def count_waiting_messages(session: AsyncSession) -> int:
        """Count messages that still have work left, derived from the same conditions
        the stages select on:

        - `embedding IS NULL` -> waiting for the embedding stage (all chatgroups);
        - `salience_score IS NULL` -> waiting for the salience stage, unless the
          chatgroup opted out (the score stays NULL by design);
        - a `waiting` event/topic/fact flag.

        Messages inside the protected window are included: they stay counted until
        their chat grows enough to make them eligible for consolidation.
        """
        stmt = (
            select(func.count())
            .select_from(MessageDO)
            .join(ChatDO, ChatDO.id == MessageDO.chat_id)
            .join(ChatGroupDO, ChatGroupDO.id == ChatDO.chatgroup_id)
            .where(
                or_(
                    MessageDO.embedding.is_(None),
                    and_(
                        MessageDO.salience_score.is_(None),
                        _use_salience_optin(),
                    ),
                    MessageDO.processing_state["event"].astext == ProcessingStatus.WAITING,
                    MessageDO.processing_state["topic"].astext == ProcessingStatus.WAITING,
                    MessageDO.processing_state["fact"].astext == ProcessingStatus.WAITING,
                )
            )
        )
        res = await session.execute(stmt)
        return res.scalar_one()
