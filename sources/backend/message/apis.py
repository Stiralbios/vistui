import uuid
from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.message.schemas import (
    MessageContext,
    MessageCreate,
    MessageCreateInternal,
    MessageFilter,
    MessageInternal,
    MessageRead,
    MessageUpdate,
    MessageUpdateInternal,
    default_processing_state,
)
from backend.message.services import MessageService
from backend.schemas import CursorPage
from backend.user.schemas import UserInternal
from fastapi import APIRouter, Depends, status

router = APIRouter(prefix="/api/v1/chats/{chat_id}/messages", tags=["message"])


@router.post("", response_model=MessageRead, status_code=status.HTTP_201_CREATED)
async def create_message(
    chat_id: uuid.UUID,
    message: MessageCreate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> MessageInternal:
    message_internal = MessageCreateInternal.model_validate(
        {
            **message.model_dump(exclude_unset=True),
            "id": uuid.uuid4(),
            "chat_id": chat_id,
            "prev_message_id": None,
            "processing_state": default_processing_state(),
            "context": {"user_id": current_user.id},
        }
    )
    return await MessageService().create(message_internal)


@router.patch("/{message_id}", response_model=MessageRead)
async def update_message(
    chat_id: uuid.UUID,
    message_id: uuid.UUID,
    message: MessageUpdate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> MessageInternal:
    context = MessageContext(user_id=current_user.id)
    message_internal = MessageUpdateInternal.model_validate(
        {**message.model_dump(exclude_unset=True), "id": message_id, "chat_id": chat_id, "context": context}
    )
    return await MessageService().update(message_internal)


@router.get("/{message_id}", response_model=MessageRead)
async def retrieve_message(
    chat_id: uuid.UUID,
    message_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> MessageInternal:
    return await MessageService().retrieve(message_id, chat_id, current_user.id)


@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    chat_id: uuid.UUID,
    message_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await MessageService().delete(message_id, chat_id, current_user.id)


@router.get("", response_model=CursorPage[MessageRead])
async def list_messages(
    chat_id: uuid.UUID,
    message_filter: Annotated[MessageFilter, Depends()],
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> CursorPage[MessageRead]:
    items = await MessageService().list_by_chat(chat_id, current_user.id, message_filter)
    has_more = len(items) > message_filter.limit
    items = items[: message_filter.limit]
    next_cursor = items[-1].id if items and has_more else None
    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)
