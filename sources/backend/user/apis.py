from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.user.schemas import UserInternal, UserRead
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api/v1/users", tags=["user"])


@router.get(path="/me", response_model=UserRead)
async def retrieve_user_me(
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
):
    return current_user
