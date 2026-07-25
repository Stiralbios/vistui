import uuid

from backend.database import with_async_session
from backend.exceptions import FactAlreadyExistProblem, FactNotFoundProblem
from backend.fact.models import FactDO, FactMessageLinkDO
from backend.fact.schemas import FactCreateInternal, FactInternal, FactUpdateInternal
from pydantic import UUID4
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _messages_linked_count():
    return (
        select(func.coalesce(func.count(FactMessageLinkDO.message_id), 0))
        .where(FactMessageLinkDO.fact_id == FactDO.id)
        .correlate(FactDO)
        .scalar_subquery()
    )


class FactStore:
    @staticmethod
    @with_async_session
    async def create(session: AsyncSession, fact: FactCreateInternal) -> FactInternal:
        orm_object = FactDO(**fact.model_dump(exclude={"context"}))
        session.add(orm_object)
        await session.flush()
        await session.refresh(orm_object)
        return await FactStore._load_with_count(session, orm_object.id)

    @staticmethod
    @with_async_session
    async def retrieve(session: AsyncSession, fact_id: UUID4) -> FactInternal | None:
        return await FactStore._load_with_count(session, fact_id)

    @staticmethod
    @with_async_session
    async def update(session: AsyncSession, fact: FactUpdateInternal) -> FactInternal:
        orm_object = await session.get(FactDO, fact.id)
        if orm_object is None:
            raise FactNotFoundProblem(f"Fact {fact.id} not found")

        for field, value in fact.model_dump(exclude_unset=True, exclude={"context"}).items():
            setattr(orm_object, field, value)
        await session.flush()
        await session.refresh(orm_object)
        return await FactStore._load_with_count(session, orm_object.id)

    @staticmethod
    @with_async_session
    async def delete(session: AsyncSession, fact_id: UUID4) -> None:
        orm_object = await session.get(FactDO, fact_id)
        if orm_object is None:
            raise FactNotFoundProblem(f"Fact {fact_id} not found")
        await session.delete(orm_object)
        await session.flush()

    @staticmethod
    @with_async_session
    async def list_by_chatgroup(
        session: AsyncSession,
        chatgroup_id: uuid.UUID,
        limit: int,
        cursor: uuid.UUID | None,
    ) -> list[FactInternal]:
        count_expr = _messages_linked_count()
        query = (
            select(FactDO, count_expr.label("messages_linked_count"))
            .where(FactDO.chatgroup_id == chatgroup_id)
            .order_by(FactDO.created_at.desc(), FactDO.id.desc())
        )
        if cursor is not None:
            cursor_obj = await session.get(FactDO, cursor)
            if cursor_obj is not None:
                query = query.where(
                    (FactDO.created_at < cursor_obj.created_at)
                    | ((FactDO.created_at == cursor_obj.created_at) & (FactDO.id < cursor_obj.id))
                )
        query = query.limit(limit + 1)
        res = await session.execute(query)
        rows = res.all()
        return [FactStore._row_to_internal(row) for row in rows]

    @staticmethod
    @with_async_session
    async def link_message(session: AsyncSession, fact_id: uuid.UUID, message_id: uuid.UUID) -> None:
        link = FactMessageLinkDO(fact_id=fact_id, message_id=message_id)
        session.add(link)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise FactAlreadyExistProblem(f"Message {message_id} is already linked to fact {fact_id}") from exc

    @staticmethod
    async def _load_with_count(session: AsyncSession, fact_id: uuid.UUID) -> FactInternal | None:
        count_expr = _messages_linked_count()
        query = select(FactDO, count_expr.label("messages_linked_count")).where(FactDO.id == fact_id)
        res = await session.execute(query)
        row = res.one_or_none()
        return None if row is None else FactStore._row_to_internal(row)

    @staticmethod
    def _row_to_internal(row) -> FactInternal:
        fact, count = row
        data = {column.key: getattr(fact, column.key) for column in FactDO.__table__.columns}
        data["messages_linked_count"] = count
        return FactInternal.model_validate(data)
