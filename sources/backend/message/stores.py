import datetime as dt
import uuid

from backend.database import with_async_session
from backend.exceptions import MessageNotFoundProblem
from backend.llm import Provider
from backend.message.constants import MessageRole
from backend.message.models import MessageDO
from backend.message.schemas import (
    MessageCreateInternal,
    MessageInternal,
    MessageUpdateInternal,
    default_processing_state,
)
from backend.utils.embeddings import embed_text
from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class MessageStore:
    @staticmethod
    @with_async_session
    async def create(
        session: AsyncSession, message: MessageCreateInternal, provider: Provider | None = None
    ) -> MessageInternal:
        orm_object = MessageDO(**message.model_dump(exclude={"context"}))
        orm_object.embedding = await embed_text(orm_object.full_text, provider)
        session.add(orm_object)
        await session.flush()
        await session.refresh(orm_object)
        return MessageInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def retrieve(session: AsyncSession, message_id: UUID4) -> MessageInternal | None:
        orm_object = await session.get(MessageDO, message_id)
        return None if orm_object is None else MessageInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def update(
        session: AsyncSession, message: MessageUpdateInternal, provider: Provider | None = None
    ) -> MessageInternal:
        orm_object = await session.get(MessageDO, message.id)
        if orm_object is None:
            raise MessageNotFoundProblem(f"Message {message.id} not found")

        payload = message.model_dump(exclude_unset=True, exclude={"context", "id", "chat_id"})
        text_written = "full_text" in payload
        text_changed = text_written and payload["full_text"] != orm_object.full_text
        for field, value in payload.items():
            setattr(orm_object, field, value)
        if text_written:
            # The text field was rewritten: record the write.
            orm_object.edited_at = dt.datetime.now(dt.timezone.utc)
        if text_changed:
            # The content really changed: recompute its embedding and requeue
            # everything derived from the text.
            orm_object.embedding = await embed_text(orm_object.full_text, provider)
            orm_object.salience_score = None
            orm_object.processing_state = default_processing_state()
        await session.flush()
        await session.refresh(orm_object)
        return MessageInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def delete(session: AsyncSession, message_id: UUID4) -> None:
        orm_object = await session.get(MessageDO, message_id)
        if orm_object is None:
            raise MessageNotFoundProblem(f"Message {message_id} not found")
        await session.delete(orm_object)
        await session.flush()

    @staticmethod
    @with_async_session
    async def get_last_message_id(session: AsyncSession, chat_id: uuid.UUID) -> uuid.UUID | None:
        stmt = (
            select(MessageDO.id)
            .where(MessageDO.chat_id == chat_id)
            .order_by(MessageDO.created_at.desc(), MessageDO.id.desc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    @with_async_session
    async def list_by_chat(
        session: AsyncSession,
        chat_id: uuid.UUID,
        limit: int,
        cursor: uuid.UUID | None,
        role: MessageRole | None,
    ) -> list[MessageInternal]:
        query = select(MessageDO).where(MessageDO.chat_id == chat_id)
        if role is not None:
            query = query.where(MessageDO.role == role)
        if cursor is not None:
            cursor_obj = await session.get(MessageDO, cursor)
            if cursor_obj is not None:
                query = query.where(
                    (MessageDO.created_at < cursor_obj.created_at)
                    | ((MessageDO.created_at == cursor_obj.created_at) & (MessageDO.id < cursor_obj.id))
                )
        query = query.order_by(MessageDO.created_at.desc(), MessageDO.id.desc()).limit(limit + 1)
        res = await session.execute(query)
        orm_objects = res.scalars().all()
        return [MessageInternal.model_validate(orm_object) for orm_object in orm_objects]
