import uuid
from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.event.schemas import (
    EventContext,
    EventCreate,
    EventCreateInternal,
    EventFilter,
    EventInternal,
    EventRead,
    EventUpdate,
    EventUpdateInternal,
)
from backend.event.services import EventService
from backend.schemas import CursorPage
from backend.user.schemas import UserInternal
from fastapi import APIRouter, Depends, status

router = APIRouter(prefix="/api/v1/events", tags=["event"])


@router.post("", response_model=EventRead, status_code=status.HTTP_201_CREATED)
async def create_event(
    event: EventCreate,
    chat_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> EventInternal:
    event_internal = EventCreateInternal.model_validate(
        {**event.model_dump(exclude_unset=True), "chat_id": chat_id, "context": {"user_id": current_user.id}}
    )
    return await EventService().create(event_internal)


@router.get("/{event_id}", response_model=EventRead)
async def retrieve_event(
    event_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> EventInternal:
    return await EventService().retrieve(event_id, current_user.id)


@router.patch("/{event_id}", response_model=EventRead)
async def update_event(
    event_id: uuid.UUID,
    event: EventUpdate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> EventInternal:
    context = EventContext(user_id=current_user.id)
    event_internal = EventUpdateInternal.model_validate(
        {**event.model_dump(exclude_unset=True), "id": event_id, "context": context}
    )
    return await EventService().update(event_internal)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await EventService().delete(event_id, current_user.id)


@router.get("/chats/{chat_id}/events", response_model=CursorPage[EventRead])
async def list_events_by_chat(
    chat_id: uuid.UUID,
    event_filter: Annotated[EventFilter, Depends()],
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> CursorPage[EventRead]:
    items = await EventService().list_by_chat(chat_id, current_user.id, event_filter)
    has_more = len(items) > event_filter.limit
    items = items[: event_filter.limit]
    next_cursor = items[-1].id if items and has_more else None
    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)


@router.post("/{event_id}/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def link_message_to_event(
    event_id: uuid.UUID,
    message_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await EventService().link_message(event_id, message_id, current_user.id)
