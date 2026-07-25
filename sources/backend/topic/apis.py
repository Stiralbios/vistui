import uuid
from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.schemas import CursorPage
from backend.topic.schemas import (
    TopicContext,
    TopicCreate,
    TopicCreateInternal,
    TopicFilter,
    TopicInternal,
    TopicRead,
    TopicUpdate,
    TopicUpdateInternal,
)
from backend.topic.services import TopicService
from backend.user.schemas import UserInternal
from fastapi import APIRouter, Depends, status

router = APIRouter(prefix="/api/v1/topics", tags=["topic"])


@router.post("", response_model=TopicRead, status_code=status.HTTP_201_CREATED)
async def create_topic(
    topic: TopicCreate,
    chatgroup_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> TopicInternal:
    topic_internal = TopicCreateInternal.model_validate(
        {**topic.model_dump(exclude_unset=True), "chatgroup_id": chatgroup_id, "context": {"user_id": current_user.id}}
    )
    return await TopicService().create(topic_internal)


@router.get("/{topic_id}", response_model=TopicRead)
async def retrieve_topic(
    topic_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> TopicInternal:
    return await TopicService().retrieve(topic_id, current_user.id)


@router.patch("/{topic_id}", response_model=TopicRead)
async def update_topic(
    topic_id: uuid.UUID,
    topic: TopicUpdate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> TopicInternal:
    context = TopicContext(user_id=current_user.id)
    topic_internal = TopicUpdateInternal.model_validate(
        {**topic.model_dump(exclude_unset=True), "id": topic_id, "context": context}
    )
    return await TopicService().update(topic_internal)


@router.delete("/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(
    topic_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await TopicService().delete(topic_id, current_user.id)


@router.get("/chatgroups/{chatgroup_id}/topics", response_model=CursorPage[TopicRead])
async def list_topics_by_chatgroup(
    chatgroup_id: uuid.UUID,
    topic_filter: Annotated[TopicFilter, Depends()],
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> CursorPage[TopicRead]:
    items = await TopicService().list_by_chatgroup(chatgroup_id, current_user.id, topic_filter)
    has_more = len(items) > topic_filter.limit
    items = items[: topic_filter.limit]
    next_cursor = items[-1].id if items and has_more else None
    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)


@router.post("/{topic_id}/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def link_message_to_topic(
    topic_id: uuid.UUID,
    message_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await TopicService().link_message(topic_id, message_id, current_user.id)
