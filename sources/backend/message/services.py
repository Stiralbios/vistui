import uuid

from backend.chat.stores import ChatStore
from backend.chatgroup.stores import ChatGroupStore
from backend.exceptions import (
    ChatNotAllowedProblem,
    ChatNotFoundProblem,
    MessageNotFoundProblem,
)
from backend.message.schemas import (
    MessageCreateInternal,
    MessageFilter,
    MessageInternal,
    MessageUpdateInternal,
    default_processing_state,
)
from backend.message.stores import MessageStore


class MessageService:
    def __init__(self) -> None:
        self.store = MessageStore
        self.chat_store = ChatStore
        self.chatgroup_store = ChatGroupStore

    async def create(self, message: MessageCreateInternal) -> MessageInternal:
        await self._ensure_chat_access(message.chat_id, message.context.user_id)
        last_message_id = await self.store.get_last_message_id(message.chat_id)
        message.prev_message_id = last_message_id
        message.processing_state = default_processing_state()
        return await self.store.create(message)

    async def retrieve(self, message_id: uuid.UUID, chat_id: uuid.UUID, user_id: uuid.UUID) -> MessageInternal:
        message = await self.store.retrieve(message_id)
        await self._ensure_message_in_chat(message, chat_id, user_id)
        return message

    async def update(self, message: MessageUpdateInternal) -> MessageInternal:
        await self._ensure_chat_access(message.chat_id, message.context.user_id)
        existing = await self.store.retrieve(message.id)
        if existing is None or existing.chat_id != message.chat_id:
            raise MessageNotFoundProblem(f"Message {message.id} not found")
        return await self.store.update(message)

    async def delete(self, message_id: uuid.UUID, chat_id: uuid.UUID, user_id: uuid.UUID) -> None:
        existing = await self.store.retrieve(message_id)
        await self._ensure_message_in_chat(existing, chat_id, user_id)
        await self.store.delete(message_id)

    async def list_by_chat(
        self,
        chat_id: uuid.UUID,
        user_id: uuid.UUID,
        message_filter: MessageFilter,
    ) -> list[MessageInternal]:
        await self._ensure_chat_access(chat_id, user_id)
        return await self.store.list_by_chat(chat_id, message_filter.limit, message_filter.cursor, message_filter.role)

    async def _ensure_chat_access(self, chat_id: uuid.UUID, user_id: uuid.UUID) -> None:
        chat = await self.chat_store.retrieve(chat_id)
        if chat is None:
            raise ChatNotFoundProblem(f"Chat {chat_id} not found")
        chatgroup = await self.chatgroup_store.retrieve(chat.chatgroup_id)
        if chatgroup is None or chatgroup.user_id != user_id:
            raise ChatNotAllowedProblem(f"User {user_id} is not allowed to access chat {chat_id}")

    async def _ensure_message_in_chat(
        self, message: MessageInternal | None, chat_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        await self._ensure_chat_access(chat_id, user_id)
        if message is None or message.chat_id != chat_id:
            raise MessageNotFoundProblem("Message not found")
