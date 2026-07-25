import uuid

from backend.database import with_async_session
from backend.exceptions import TopicAlreadyExistProblem, TopicNotFoundProblem
from backend.topic.models import TopicDO, TopicMessageLinkDO
from backend.topic.schemas import TopicCreateInternal, TopicInternal, TopicUpdateInternal
from pydantic import UUID4
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _messages_linked_count():
    return (
        select(func.coalesce(func.count(TopicMessageLinkDO.message_id), 0))
        .where(TopicMessageLinkDO.topic_id == TopicDO.id)
        .correlate(TopicDO)
        .scalar_subquery()
    )


class TopicStore:
    @staticmethod
    @with_async_session
    async def create(session: AsyncSession, topic: TopicCreateInternal) -> TopicInternal:
        orm_object = TopicDO(**topic.model_dump(exclude={"context"}))
        session.add(orm_object)
        await session.flush()
        await session.refresh(orm_object)
        return await TopicStore._load_with_count(session, orm_object.id)

    @staticmethod
    @with_async_session
    async def retrieve(session: AsyncSession, topic_id: UUID4) -> TopicInternal | None:
        return await TopicStore._load_with_count(session, topic_id)

    @staticmethod
    @with_async_session
    async def update(session: AsyncSession, topic: TopicUpdateInternal) -> TopicInternal:
        orm_object = await session.get(TopicDO, topic.id)
        if orm_object is None:
            raise TopicNotFoundProblem(f"Topic {topic.id} not found")

        for field, value in topic.model_dump(exclude_unset=True, exclude={"context"}).items():
            setattr(orm_object, field, value)
        await session.flush()
        await session.refresh(orm_object)
        return await TopicStore._load_with_count(session, orm_object.id)

    @staticmethod
    @with_async_session
    async def delete(session: AsyncSession, topic_id: UUID4) -> None:
        orm_object = await session.get(TopicDO, topic_id)
        if orm_object is None:
            raise TopicNotFoundProblem(f"Topic {topic_id} not found")
        await session.delete(orm_object)
        await session.flush()

    @staticmethod
    @with_async_session
    async def list_by_chatgroup(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        limit: int,
        cursor: uuid.UUID | None,
    ) -> list[TopicInternal]:
        count_expr = _messages_linked_count()
        query = (
            select(TopicDO, count_expr.label("messages_linked_count"))
            .where(TopicDO.chatgroup_id == chatgroup_id)
            .order_by(TopicDO.created_at.desc(), TopicDO.id.desc())
        )
        if cursor is not None:
            cursor_obj = await session.get(TopicDO, cursor)
            if cursor_obj is not None:
                query = query.where(
                    (TopicDO.created_at < cursor_obj.created_at)
                    | ((TopicDO.created_at == cursor_obj.created_at) & (TopicDO.id < cursor_obj.id))
                )
        query = query.limit(limit + 1)
        res = await session.execute(query)
        rows = res.all()
        return [TopicStore._row_to_internal(row) for row in rows]

    @staticmethod
    @with_async_session
    async def link_message(session: AsyncSession, topic_id: uuid.UUID, message_id: uuid.UUID) -> None:
        link = TopicMessageLinkDO(topic_id=topic_id, message_id=message_id)
        session.add(link)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise TopicAlreadyExistProblem(f"Message {message_id} is already linked to topic {topic_id}") from exc

    @staticmethod
    async def _load_with_count(session: AsyncSession, topic_id: uuid.UUID) -> TopicInternal | None:
        count_expr = _messages_linked_count()
        query = select(TopicDO, count_expr.label("messages_linked_count")).where(TopicDO.id == topic_id)
        res = await session.execute(query)
        row = res.one_or_none()
        return None if row is None else TopicStore._row_to_internal(row)

    @staticmethod
    def _row_to_internal(row) -> TopicInternal:
        topic, count = row
        data = {column.key: getattr(topic, column.key) for column in TopicDO.__table__.columns}
        data["messages_linked_count"] = count
        return TopicInternal.model_validate(data)
