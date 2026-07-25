from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.user.schemas import UserCreate, UserCreateInternal, UserInternal, UserRead
from backend.user.services import UserService
from backend.utils.security import hash_password
from fastapi import APIRouter, Depends, status

router = APIRouter(prefix="/api/v1/users", tags=["user"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    user: UserCreate,
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
) -> UserInternal:
    hashed_password = hash_password(user.password)
    user_internal = UserCreateInternal.model_validate(
        {"hashed_password": hashed_password, **user.model_dump(exclude={"password"}, exclude_unset=True)}
    )
    return await UserService().create(user_internal)


@router.get(path="/me", response_model=UserRead)
async def retrieve_user_me(
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
):
    return current_user
