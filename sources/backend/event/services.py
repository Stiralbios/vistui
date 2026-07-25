import uuid

from backend.chat.stores import ChatStore
from backend.chatgroup.stores import ChatGroupStore
from backend.event.schemas import EventCreateInternal, EventFilter, EventInternal, EventUpdateInternal
from backend.event.stores import EventStore
from backend.exceptions import ChatNotAllowedProblem, ChatNotFoundProblem, EventNotFoundProblem


class EventService:
    def __init__(self) -> None:
        self.store = EventStore
        self.chat_store = ChatStore
        self.chatgroup_store = ChatGroupStore

    async def create(self, event: EventCreateInternal) -> EventInternal:
        await self._ensure_chat_access(event.chat_id, event.context.user_id)
        return await self.store.create(event)

    async def retrieve(self, event_id: uuid.UUID, user_id: uuid.UUID) -> EventInternal:
        event = await self.store.retrieve(event_id)
        await self._ensure_event_access(event, user_id)
        return event

    async def update(self, event: EventUpdateInternal) -> EventInternal:
        existing = await self.store.retrieve(event.id)
        await self._ensure_event_access(existing, event.context.user_id)
        return await self.store.update(event)

    async def delete(self, event_id: uuid.UUID, user_id: uuid.UUID) -> None:
        existing = await self.store.retrieve(event_id)
        await self._ensure_event_access(existing, user_id)
        await self.store.delete(event_id)

    async def list_by_chat(
        self, chat_id: uuid.UUID, user_id: uuid.UUID, event_filter: EventFilter
    ) -> list[EventInternal]:
        await self._ensure_chat_access(chat_id, user_id)
        return await self.store.list_by_chat(chat_id, event_filter.limit, event_filter.cursor)

    async def link_message(self, event_id: uuid.UUID, message_id: uuid.UUID, user_id: uuid.UUID) -> None:
        event = await self.store.retrieve(event_id)
        await self._ensure_event_access(event, user_id)
        await self.store.link_message(event_id, message_id)

    async def _ensure_chat_access(self, chat_id: uuid.UUID, user_id: uuid.UUID) -> None:
        chat = await self.chat_store.retrieve(chat_id)
        if chat is None:
            raise ChatNotFoundProblem(f"Chat {chat_id} not found")
        chatgroup = await self.chatgroup_store.retrieve(chat.chatgroup_id)
        if chatgroup is None or chatgroup.user_id != user_id:
            raise ChatNotAllowedProblem(f"User {user_id} is not allowed to access chat {chat_id}")

    async def _ensure_event_access(self, event: EventInternal | None, user_id: uuid.UUID) -> None:
        if event is None:
            raise EventNotFoundProblem("Event not found")
        await self._ensure_chat_access(event.chat_id, user_id)
