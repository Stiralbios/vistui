import uuid

from backend.chat.schemas import (
    ChatCreateInternal,
    ChatFilter,
    ChatInternal,
    ChatUpdateInternal,
)
from backend.chat.stores import ChatStore
from backend.chatgroup.schemas import (
    ChatGroupConfig,
    ChatGroupCreateInternal,
    ChatGroupPrompts,
)
from backend.chatgroup.stores import ChatGroupStore
from backend.exceptions import ChatGroupNotFoundProblem, ChatNotAllowedProblem, ChatNotFoundProblem


class ChatService:
    def __init__(self) -> None:
        self.store = ChatStore
        self.chatgroup_store = ChatGroupStore

    async def create(self, chat: ChatCreateInternal) -> ChatInternal:
        user_id = chat.context.user_id
        chatgroup_id = chat.chatgroup_id

        if chatgroup_id is None:
            chatgroup = await self.chatgroup_store.create(
                ChatGroupCreateInternal.model_validate(
                    {
                        "name": chat.name,
                        "description": chat.description,
                        "user_id": user_id,
                        "config": ChatGroupConfig(),
                        "prompts": ChatGroupPrompts(),
                        "context": {"user_id": user_id},
                    }
                )
            )
            chatgroup_id = chatgroup.id
        else:
            chatgroup = await self.chatgroup_store.retrieve(chatgroup_id)
            if chatgroup is None or chatgroup.user_id != user_id:
                raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup_id} not found")

        chat_create = ChatCreateInternal.model_validate(
            {
                **chat.model_dump(exclude={"context"}, exclude_unset=True),
                "chatgroup_id": chatgroup_id,
                "context": {"user_id": user_id},
            }
        )
        return await self.store.create(chat_create)

    async def retrieve(self, chat_id: uuid.UUID, user_id: uuid.UUID) -> ChatInternal:
        chat = await self.store.retrieve(chat_id)
        await self._ensure_owner(chat, user_id)
        return chat

    async def update(self, chat: ChatUpdateInternal) -> ChatInternal:
        existing = await self.store.retrieve(chat.id)
        await self._ensure_owner(existing, chat.context.user_id)
        return await self.store.update(chat)

    async def delete(self, chat_id: uuid.UUID, user_id: uuid.UUID) -> None:
        existing = await self.store.retrieve(chat_id)
        await self._ensure_owner(existing, user_id)
        await self.store.delete(chat_id)

    async def list_by_chatgroup(
        self, chatgroup_id: uuid.UUID, user_id: uuid.UUID, chat_filter: ChatFilter
    ) -> list[ChatInternal]:
        chatgroup = await self.chatgroup_store.retrieve(chatgroup_id)
        if chatgroup is None or chatgroup.user_id != user_id:
            raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup_id} not found")
        return await self.store.list_by_chatgroup(chatgroup_id, chat_filter.limit, chat_filter.cursor)

    async def _ensure_owner(self, chat: ChatInternal | None, user_id: uuid.UUID) -> None:
        if chat is None:
            raise ChatNotFoundProblem("Chat not found")
        chatgroup = await self.chatgroup_store.retrieve(chat.chatgroup_id)
        if chatgroup is None or chatgroup.user_id != user_id:
            raise ChatNotAllowedProblem(f"User {user_id} is not allowed to access chat {chat.id}")
