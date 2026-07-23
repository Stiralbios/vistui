import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr


class UserBase(BaseModel):
    name: str = Field(examples=["Alice"])
    email: EmailStr = Field(examples=["alice@example.com"])


class UserRead(UserBase):
    id: uuid.UUID
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None


class UserInternal(UserRead):
    model_config = ConfigDict(from_attributes=True)

    hashed_password: str


class UserCreate(UserBase):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(examples=["mygreatpassword"])


class UserCreateInternal(UserBase):
    model_config = ConfigDict(extra="forbid")

    is_active: bool = True
    hashed_password: str
