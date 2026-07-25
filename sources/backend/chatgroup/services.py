import uuid

from backend.chatgroup.schemas import (
    ChatGroupCreateInternal,
    ChatGroupFilter,
    ChatGroupInternal,
    ChatGroupUpdateInternal,
)
from backend.chatgroup.stores import ChatGroupStore
from backend.exceptions import ChatGroupNotFoundProblem


class ChatGroupService:
    def __init__(self) -> None:
        self.store = ChatGroupStore

    async def create(self, chatgroup: ChatGroupCreateInternal) -> ChatGroupInternal:
        return await self.store.create(chatgroup)

    async def retrieve(self, chatgroup_id: uuid.UUID, user_id: uuid.UUID) -> ChatGroupInternal:
        chatgroup = await self.store.retrieve(chatgroup_id)
        if chatgroup is None or chatgroup.user_id != user_id:
            raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup_id} not found")
        return chatgroup

    async def update(self, chatgroup: ChatGroupUpdateInternal) -> ChatGroupInternal:
        await self.retrieve(chatgroup.id, chatgroup.context.user_id)
        return await self.store.update(chatgroup)

    async def delete(self, chatgroup_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.retrieve(chatgroup_id, user_id)
        await self.store.delete(chatgroup_id)

    async def list(self, user_id: uuid.UUID, chatgroup_filter: ChatGroupFilter) -> list[ChatGroupInternal]:
        return await self.store.list(user_id, chatgroup_filter.limit, chatgroup_filter.cursor)
