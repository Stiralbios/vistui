import uuid

from backend.database import with_async_session
from backend.event.models import EventDO, EventMessageLinkDO
from backend.event.schemas import EventCreateInternal, EventInternal, EventUpdateInternal
from backend.exceptions import EventAlreadyExistProblem, EventNotFoundProblem
from pydantic import UUID4
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _messages_linked_count():
    return (
        select(func.coalesce(func.count(EventMessageLinkDO.message_id), 0))
        .where(EventMessageLinkDO.event_id == EventDO.id)
        .correlate(EventDO)
        .scalar_subquery()
    )


class EventStore:
    @staticmethod
    @with_async_session
    async def create(session: AsyncSession, event: EventCreateInternal) -> EventInternal:
        orm_object = EventDO(**event.model_dump(exclude={"context"}))
        session.add(orm_object)
        await session.flush()
        await session.refresh(orm_object)
        return await EventStore._load_with_count(session, orm_object.id)

    @staticmethod
    @with_async_session
    async def retrieve(session: AsyncSession, event_id: UUID4) -> EventInternal | None:
        return await EventStore._load_with_count(session, event_id)

    @staticmethod
    @with_async_session
    async def update(session: AsyncSession, event: EventUpdateInternal) -> EventInternal:
        orm_object = await session.get(EventDO, event.id)
        if orm_object is None:
            raise EventNotFoundProblem(f"Event {event.id} not found")

        for field, value in event.model_dump(exclude_unset=True, exclude={"context"}).items():
            setattr(orm_object, field, value)
        await session.flush()
        await session.refresh(orm_object)
        return await EventStore._load_with_count(session, orm_object.id)

    @staticmethod
    @with_async_session
    async def delete(session: AsyncSession, event_id: UUID4) -> None:
        orm_object = await session.get(EventDO, event_id)
        if orm_object is None:
            raise EventNotFoundProblem(f"Event {event_id} not found")
        await session.delete(orm_object)
        await session.flush()

    @staticmethod
    @with_async_session
    async def list_by_chat(
        session: AsyncSession,
        chat_id: uuid.UUID,
        limit: int,
        cursor: uuid.UUID | None,
    ) -> list[EventInternal]:
        count_expr = _messages_linked_count()
        query = (
            select(EventDO, count_expr.label("messages_linked_count"))
            .where(EventDO.chat_id == chat_id)
            .order_by(EventDO.created_at.desc(), EventDO.id.desc())
        )
        if cursor is not None:
            cursor_obj = await session.get(EventDO, cursor)
            if cursor_obj is not None:
                query = query.where(
                    (EventDO.created_at < cursor_obj.created_at)
                    | ((EventDO.created_at == cursor_obj.created_at) & (EventDO.id < cursor_obj.id))
                )
        query = query.limit(limit + 1)
        res = await session.execute(query)
        rows = res.all()
        return [EventStore._row_to_internal(row) for row in rows]

    @staticmethod
    @with_async_session
    async def link_message(session: AsyncSession, event_id: uuid.UUID, message_id: uuid.UUID) -> None:
        link = EventMessageLinkDO(event_id=event_id, message_id=message_id)
        session.add(link)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise EventAlreadyExistProblem(f"Message {message_id} is already linked to event {event_id}") from exc

    @staticmethod
    async def _load_with_count(session: AsyncSession, event_id: uuid.UUID) -> EventInternal | None:
        count_expr = _messages_linked_count()
        query = select(EventDO, count_expr.label("messages_linked_count")).where(EventDO.id == event_id)
        res = await session.execute(query)
        row = res.one_or_none()
        return None if row is None else EventStore._row_to_internal(row)

    @staticmethod
    def _row_to_internal(row) -> EventInternal:
        event, count = row
        data = {column.key: getattr(event, column.key) for column in EventDO.__table__.columns}
        data["messages_linked_count"] = count
        return EventInternal.model_validate(data)
