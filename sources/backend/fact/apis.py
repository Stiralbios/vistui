import uuid
from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.fact.schemas import (
    FactContext,
    FactCreate,
    FactCreateInternal,
    FactFilter,
    FactInternal,
    FactRead,
    FactUpdate,
    FactUpdateInternal,
)
from backend.fact.services import FactService
from backend.schemas import CursorPage
from backend.user.schemas import UserInternal
from fastapi import APIRouter, Depends, status

router = APIRouter(prefix="/api/v1/facts", tags=["fact"])


@router.post("", response_model=FactRead, status_code=status.HTTP_201_CREATED)
async def create_fact(
    fact: FactCreate,
    chatgroup_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> FactInternal:
    fact_internal = FactCreateInternal.model_validate(
        {**fact.model_dump(exclude_unset=True), "chatgroup_id": chatgroup_id, "context": {"user_id": current_user.id}}
    )
    return await FactService().create(fact_internal)


@router.get("/{fact_id}", response_model=FactRead)
async def retrieve_fact(
    fact_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> FactInternal:
    return await FactService().retrieve(fact_id, current_user.id)


@router.patch("/{fact_id}", response_model=FactRead)
async def update_fact(
    fact_id: uuid.UUID,
    fact: FactUpdate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> FactInternal:
    context = FactContext(user_id=current_user.id)
    fact_internal = FactUpdateInternal.model_validate(
        {**fact.model_dump(exclude_unset=True), "id": fact_id, "context": context}
    )
    return await FactService().update(fact_internal)


@router.delete("/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fact(
    fact_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await FactService().delete(fact_id, current_user.id)


@router.get("/chatgroups/{chatgroup_id}/facts", response_model=CursorPage[FactRead])
async def list_facts_by_chatgroup(
    chatgroup_id: uuid.UUID,
    fact_filter: Annotated[FactFilter, Depends()],
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> CursorPage[FactRead]:
    items = await FactService().list_by_chatgroup(chatgroup_id, current_user.id, fact_filter)
    has_more = len(items) > fact_filter.limit
    items = items[: fact_filter.limit]
    next_cursor = items[-1].id if items and has_more else None
    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)


@router.post("/{fact_id}/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def link_message_to_fact(
    fact_id: uuid.UUID,
    message_id: uuid.UUID,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> None:
    await FactService().link_message(fact_id, message_id, current_user.id)
