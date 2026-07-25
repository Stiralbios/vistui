import uuid
from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.chat.schemas import (
    ChatContext,
    ChatCreate,
    ChatCreateInternal,
    ChatFilter,
    ChatInternal,
    ChatRead,
    ChatUpdate,
    ChatUpdateInternal,
)
from backend.chat.services import ChatService
from backend.schemas import CursorPage
from backend.user.schemas import UserInternal
from fastapi import APIRouter, Depends, status

router = APIRouter(prefix="/api/v1/chats", tags=["chat"])


@router.post("", response_model=ChatRead, status_code=status.HTTP_201_CREATED)
async def create_chat(
    chat: ChatCreate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> ChatInternal:
    chat_internal = ChatCreateInternal.model_validate(
        {**chat.model_dump(exclude_unset=True), "context": {"user_id": current_user.id}}
    )
    return await ChatService().create(chat_internal)


@router.get("/{chat_id}", response_model=ChatRead)
async def retrieve_chat(
    chat_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> ChatInternal:
    return await ChatService().retrieve(chat_id, current_user.id)


@router.patch("/{chat_id}", response_model=ChatRead)
async def update_chat(
    chat_id: uuid.UUID,
    chat: ChatUpdate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> ChatInternal:
    context = ChatContext(user_id=current_user.id)
    chat_internal = ChatUpdateInternal.model_validate(
        {**chat.model_dump(exclude_unset=True), "id": chat_id, "context": context}
    )
    return await ChatService().update(chat_internal)


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    chat_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await ChatService().delete(chat_id, current_user.id)


@router.get("/chatgroups/{chatgroup_id}/chats", response_model=CursorPage[ChatRead])
async def list_chats_by_chatgroup(
    chatgroup_id: uuid.UUID,
    chat_filter: Annotated[ChatFilter, Depends()],
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> CursorPage[ChatRead]:
    items = await ChatService().list_by_chatgroup(chatgroup_id, current_user.id, chat_filter)
    has_more = len(items) > chat_filter.limit
    items = items[: chat_filter.limit]
    next_cursor = items[-1].id if items and has_more else None
    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)
