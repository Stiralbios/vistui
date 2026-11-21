"""Search API endpoints."""

import uuid
from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.chatgroup.services import ChatGroupService
from backend.search.schemas import SearchResponse
from backend.search.service import search_chatgroup
from backend.user.schemas import UserInternal
from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/api/v1/chatgroups", tags=["search"])


@router.get("/{chatgroup_id}/search", response_model=SearchResponse)
async def search(
    chatgroup_id: uuid.UUID,
    q: Annotated[str, Query(min_length=1, max_length=512, pattern=r"\S")],
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
    k: Annotated[int, Query(ge=1, le=100)] = 20,
    token_budget: Annotated[int | None, Query(ge=1)] = None,
) -> SearchResponse:
    """Hybrid (BM25 + vector) search across all memory types of one chatgroup.

    The hits are packed into `token_budget` tokens (the chatgroup config default when
    the parameter is absent), split per memory type by the chatgroup's memory ratio.
    """
    chatgroup = await ChatGroupService().retrieve(chatgroup_id, current_user.id)
    budget = token_budget if token_budget is not None else chatgroup.config.token_budget
    return await search_chatgroup(chatgroup, q, k, budget)
