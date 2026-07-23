from datetime import datetime, timedelta, timezone

import jwt
from backend.settings import AppSettings


class AuthService:
    async def create_access_token(self, data: dict, expires_delta: timedelta | None = None):
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(minutes=15)
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(
            to_encode, AppSettings().JWT_SECRET_KEY.get_secret_value(), algorithm=AppSettings().JWT_ALGORITHM
        )
        return encoded_jwt
