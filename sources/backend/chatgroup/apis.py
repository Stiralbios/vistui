import uuid
from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.chatgroup.schemas import (
    ChatGroupContext,
    ChatGroupCreate,
    ChatGroupCreateInternal,
    ChatGroupFilter,
    ChatGroupInternal,
    ChatGroupRead,
    ChatGroupUpdate,
    ChatGroupUpdateInternal,
)
from backend.chatgroup.services import ChatGroupService
from backend.schemas import CursorPage
from backend.user.schemas import UserInternal
from fastapi import APIRouter, Depends, status

router = APIRouter(prefix="/api/v1/chatgroups", tags=["chatgroup"])


@router.post("", response_model=ChatGroupRead, status_code=status.HTTP_201_CREATED)
async def create_chatgroup(
    chatgroup: ChatGroupCreate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> ChatGroupInternal:
    chatgroup_internal = ChatGroupCreateInternal.model_validate(
        {
            **chatgroup.model_dump(exclude_unset=True),
            "user_id": current_user.id,
            "context": {"user_id": current_user.id},
        }
    )
    return await ChatGroupService().create(chatgroup_internal)


@router.get("/{chatgroup_id}", response_model=ChatGroupRead)
async def retrieve_chatgroup(
    chatgroup_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> ChatGroupInternal:
    return await ChatGroupService().retrieve(chatgroup_id, current_user.id)


@router.patch("/{chatgroup_id}", response_model=ChatGroupRead)
async def update_chatgroup(
    chatgroup_id: uuid.UUID,
    chatgroup: ChatGroupUpdate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> ChatGroupInternal:
    context = ChatGroupContext(user_id=current_user.id)
    chatgroup_internal = ChatGroupUpdateInternal.model_validate(
        {**chatgroup.model_dump(exclude_unset=True), "id": chatgroup_id, "context": context}
    )
    return await ChatGroupService().update(chatgroup_internal)


@router.delete("/{chatgroup_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chatgroup(
    chatgroup_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await ChatGroupService().delete(chatgroup_id, current_user.id)


@router.get("", response_model=CursorPage[ChatGroupRead])
async def list_chatgroups(
    chatgroup_filter: Annotated[ChatGroupFilter, Depends()],
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> CursorPage[ChatGroupRead]:
    items = await ChatGroupService().list(current_user.id, chatgroup_filter)
    has_more = len(items) > chatgroup_filter.limit
    items = items[: chatgroup_filter.limit]
    next_cursor = items[-1].id if items and has_more else None
    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)
