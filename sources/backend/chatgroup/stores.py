import uuid

from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupCreateInternal, ChatGroupInternal, ChatGroupUpdateInternal
from backend.database import with_async_session
from backend.exceptions import ChatGroupNotFoundProblem
from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class ChatGroupStore:
    @staticmethod
    @with_async_session
    async def create(session: AsyncSession, chatgroup: ChatGroupCreateInternal) -> ChatGroupInternal:
        orm_object = ChatGroupDO(**chatgroup.model_dump(exclude={"context"}))
        session.add(orm_object)
        await session.flush()
        await session.refresh(orm_object)
        return ChatGroupInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def retrieve(session: AsyncSession, chatgroup_id: UUID4) -> ChatGroupInternal | None:
        orm_object = await session.get(ChatGroupDO, chatgroup_id)
        return None if orm_object is None else ChatGroupInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def update(session: AsyncSession, chatgroup: ChatGroupUpdateInternal) -> ChatGroupInternal:
        orm_object = await session.get(ChatGroupDO, chatgroup.id)
        if orm_object is None:
            raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup.id} not found")

        for field, value in chatgroup.model_dump(exclude_unset=True, exclude={"context"}).items():
            setattr(orm_object, field, value)
        await session.flush()
        await session.refresh(orm_object)
        return ChatGroupInternal.model_validate(orm_object)

    @staticmethod
    @with_async_session
    async def delete(session: AsyncSession, chatgroup_id: UUID4) -> None:
        orm_object = await session.get(ChatGroupDO, chatgroup_id)
        if orm_object is None:
            raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup_id} not found")
        await session.delete(orm_object)
        await session.flush()

    @staticmethod
    @with_async_session
    async def list(
        session: AsyncSession, user_id: uuid.UUID, limit: int, cursor: uuid.UUID | None
    ) -> list[ChatGroupInternal]:
        query = (
            select(ChatGroupDO)
            .where(ChatGroupDO.user_id == user_id)
            .order_by(ChatGroupDO.created_at.desc(), ChatGroupDO.id.desc())
        )
        if cursor is not None:
            cursor_obj = await session.get(ChatGroupDO, cursor)
            if cursor_obj is not None:
                query = query.where(
                    (ChatGroupDO.created_at < cursor_obj.created_at)
                    | ((ChatGroupDO.created_at == cursor_obj.created_at) & (ChatGroupDO.id < cursor_obj.id))
                )
        query = query.limit(limit + 1)
        res = await session.execute(query)
        orm_objects = res.scalars().all()
        return [ChatGroupInternal.model_validate(orm_object) for orm_object in orm_objects]
