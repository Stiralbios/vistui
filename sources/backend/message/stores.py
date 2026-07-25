import uuid

from backend.database import with_async_session
from backend.exceptions import MessageNotFoundProblem
from backend.message.constants import MessageRole
from backend.message.models import MessageDO
from backend.message.schemas import MessageCreateInternal, MessageInternal, MessageUpdateInternal
from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class MessageStore:
    @staticmethod
    @with_async_session
    async def create(session: AsyncSession, message: MessageCreateInternal) -> MessageInternal:
        orm_object = MessageDO(**message.model_dump(exclude={"context"}))
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
    async def update(session: AsyncSession, message: MessageUpdateInternal) -> MessageInternal:
        orm_object = await session.get(MessageDO, message.id)
        if orm_object is None:
            raise MessageNotFoundProblem(f"Message {message.id} not found")

        for field, value in message.model_dump(exclude_unset=True, exclude={"context", "id", "chat_id"}).items():
            setattr(orm_object, field, value)
        orm_object.processing_state = {
            "embedding": "waiting",
            "salience": "waiting",
            "event": "waiting",
            "topic": "waiting",
            "fact": "waiting",
            "last_failure_at": None,
            "retry_count": 0,
        }
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
