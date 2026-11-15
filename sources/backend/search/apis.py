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
) -> SearchResponse:
    """Hybrid (BM25 + vector) search across all memory types of one chatgroup."""
    chatgroup = await ChatGroupService().retrieve(chatgroup_id, current_user.id)
    hits = await search_chatgroup(chatgroup, q, k)
    return SearchResponse(q=q, hits=hits)
