import uuid

from backend.chatgroup.stores import ChatGroupStore
from backend.exceptions import (
    ChatGroupNotAllowedProblem,
    ChatGroupNotFoundProblem,
    FactNotFoundProblem,
)
from backend.fact.schemas import FactCreateInternal, FactFilter, FactInternal, FactUpdateInternal
from backend.fact.stores import FactStore


class FactService:
    def __init__(self) -> None:
        self.store = FactStore
        self.chatgroup_store = ChatGroupStore

    async def create(self, fact: FactCreateInternal) -> FactInternal:
        await self._ensure_chatgroup_access(fact.chatgroup_id, fact.context.user_id)
        return await self.store.create(fact)

    async def retrieve(self, fact_id: uuid.UUID, user_id: uuid.UUID) -> FactInternal:
        fact = await self.store.retrieve(fact_id)
        await self._ensure_fact_access(fact, user_id)
        return fact

    async def update(self, fact: FactUpdateInternal) -> FactInternal:
        existing = await self.store.retrieve(fact.id)
        await self._ensure_fact_access(existing, fact.context.user_id)
        return await self.store.update(fact)

    async def delete(self, fact_id: uuid.UUID, user_id: uuid.UUID) -> None:
        existing = await self.store.retrieve(fact_id)
        await self._ensure_fact_access(existing, user_id)
        await self.store.delete(fact_id)

    async def list_by_chatgroup(
        self, chatgroup_id: uuid.UUID, user_id: uuid.UUID, fact_filter: FactFilter
    ) -> list[FactInternal]:
        await self._ensure_chatgroup_access(chatgroup_id, user_id)
        return await self.store.list_by_chatgroup(chatgroup_id, fact_filter.limit, fact_filter.cursor)

    async def link_message(self, fact_id: uuid.UUID, message_id: uuid.UUID, user_id: uuid.UUID) -> None:
        fact = await self.store.retrieve(fact_id)
        await self._ensure_fact_access(fact, user_id)
        await self.store.link_message(fact_id, message_id)

    async def _ensure_chatgroup_access(self, chatgroup_id: uuid.UUID, user_id: uuid.UUID) -> None:
        chatgroup = await self.chatgroup_store.retrieve(chatgroup_id)
        if chatgroup is None:
            raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup_id} not found")
        if chatgroup.user_id != user_id:
            raise ChatGroupNotAllowedProblem(f"User {user_id} is not allowed to access chatgroup {chatgroup_id}")

    async def _ensure_fact_access(self, fact: FactInternal | None, user_id: uuid.UUID) -> None:
        if fact is None:
            raise FactNotFoundProblem("Fact not found")
        await self._ensure_chatgroup_access(fact.chatgroup_id, user_id)
