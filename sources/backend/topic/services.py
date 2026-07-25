import uuid

from backend.chatgroup.stores import ChatGroupStore
from backend.exceptions import (
    ChatGroupNotAllowedProblem,
    ChatGroupNotFoundProblem,
    TopicNotFoundProblem,
)
from backend.topic.schemas import TopicCreateInternal, TopicFilter, TopicInternal, TopicUpdateInternal
from backend.topic.stores import TopicStore


class TopicService:
    def __init__(self) -> None:
        self.store = TopicStore
        self.chatgroup_store = ChatGroupStore

    async def create(self, topic: TopicCreateInternal) -> TopicInternal:
        await self._ensure_chatgroup_access(topic.chatgroup_id, topic.context.user_id)
        return await self.store.create(topic)

    async def retrieve(self, topic_id: uuid.UUID, user_id: uuid.UUID) -> TopicInternal:
        topic = await self.store.retrieve(topic_id)
        await self._ensure_topic_access(topic, user_id)
        return topic

    async def update(self, topic: TopicUpdateInternal) -> TopicInternal:
        existing = await self.store.retrieve(topic.id)
        await self._ensure_topic_access(existing, topic.context.user_id)
        return await self.store.update(topic)

    async def delete(self, topic_id: uuid.UUID, user_id: uuid.UUID) -> None:
        existing = await self.store.retrieve(topic_id)
        await self._ensure_topic_access(existing, user_id)
        await self.store.delete(topic_id)

    async def list_by_chatgroup(
        self, chatgroup_id: uuid.UUID, user_id: uuid.UUID, topic_filter: TopicFilter
    ) -> list[TopicInternal]:
        await self._ensure_chatgroup_access(chatgroup_id, user_id)
        return await self.store.list_by_chatgroup(chatgroup_id, topic_filter.limit, topic_filter.cursor)

    async def link_message(self, topic_id: uuid.UUID, message_id: uuid.UUID, user_id: uuid.UUID) -> None:
        topic = await self.store.retrieve(topic_id)
        await self._ensure_topic_access(topic, user_id)
        await self.store.link_message(topic_id, message_id)

    async def _ensure_chatgroup_access(self, chatgroup_id: uuid.UUID, user_id: uuid.UUID) -> None:
        chatgroup = await self.chatgroup_store.retrieve(chatgroup_id)
        if chatgroup is None:
            raise ChatGroupNotFoundProblem(f"ChatGroup {chatgroup_id} not found")
        if chatgroup.user_id != user_id:
            raise ChatGroupNotAllowedProblem(f"User {user_id} is not allowed to access chatgroup {chatgroup_id}")

    async def _ensure_topic_access(self, topic: TopicInternal | None, user_id: uuid.UUID) -> None:
        if topic is None:
            raise TopicNotFoundProblem("Topic not found")
        await self._ensure_chatgroup_access(topic.chatgroup_id, user_id)
