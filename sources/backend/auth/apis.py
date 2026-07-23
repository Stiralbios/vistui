from datetime import timedelta
from typing import Annotated

from backend.auth.dependencies import get_current_active_user
from backend.auth.schemas import Token
from backend.auth.services import AuthService
from backend.settings import AppSettings
from backend.user.schemas import UserInternal, UserRead
from backend.user.services import UserService
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import SecretStr

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/jwt/login")
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    secret_password = SecretStr(form_data.password)
    user = await UserService().authenticate(form_data.username, secret_password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=AppSettings().JWT_ACCESS_TOKEN_EXPIRATION_MINUTES)
    access_token = await AuthService().create_access_token(data={"sub": user.email}, expires_delta=access_token_expires)
    return Token(access_token=access_token, token_type="bearer")


@router.get("/me", response_model=UserRead)
async def retrieve_current_user(
    current_user: Annotated[UserInternal, Depends(get_current_active_user)],
):
    return current_user
