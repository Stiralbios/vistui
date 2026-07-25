import uuid

from backend.chat.models import ChatDO
from backend.chat.schemas import ChatCreateInternal, ChatInternal, ChatUpdateInternal
from backend.database import with_async_session
from backend.exceptions import ChatNotFoundProblem
from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class ChatStore:
    @staticmethod
    @with_async_session
    async def create(session: AsyncSession, chat: ChatCreateInternal) -> ChatInternal:
        orm_object = ChatDO(**chat.model_dump(exclude={"context"}))
        session.add(orm_object)
        await session.flush()
        await session.refresh(orm_object)
        return ChatInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def retrieve(session: AsyncSession, chat_id: UUID4) -> ChatInternal | None:
        orm_object = await session.get(ChatDO, chat_id)
        return None if orm_object is None else ChatInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def update(session: AsyncSession, chat: ChatUpdateInternal) -> ChatInternal:
        orm_object = await session.get(ChatDO, chat.id)
        if orm_object is None:
            raise ChatNotFoundProblem(f"Chat {chat.id} not found")

        for field, value in chat.model_dump(exclude_unset=True, exclude={"context"}).items():
            setattr(orm_object, field, value)
        await session.flush()
        await session.refresh(orm_object)
        return ChatInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def delete(session: AsyncSession, chat_id: UUID4) -> None:
        orm_object = await session.get(ChatDO, chat_id)
        if orm_object is None:
            raise ChatNotFoundProblem(f"Chat {chat_id} not found")
        await session.delete(orm_object)
        await session.flush()

    @staticmethod
    @with_async_session
    async def list_by_chatgroup(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        limit: int,
        cursor: uuid.UUID | None,
    ) -> list[ChatInternal]:
        query = (
            select(ChatDO)
            .where(ChatDO.chatgroup_id == chatgroup_id)
            .order_by(ChatDO.created_at.desc(), ChatDO.id.desc())
        )
        if cursor is not None:
            cursor_obj = await session.get(ChatDO, cursor)
            if cursor_obj is not None:
                query = query.where(
                    (ChatDO.created_at < cursor_obj.created_at)
                    | ((ChatDO.created_at == cursor_obj.created_at) & (ChatDO.id < cursor_obj.id))
                )
        query = query.limit(limit + 1)
        res = await session.execute(query)
        orm_objects = res.scalars().all()
        return [ChatInternal.model_validate(orm_object) for orm_object in orm_objects]
