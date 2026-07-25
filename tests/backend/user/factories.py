import itertools
import uuid

from backend.user.models import UserDO
from backend.user.schemas import UserRead
from backend.utils.security import hash_password
from polyfactory import Use

from tests.backend.config import BaseFactory

_name_counter = itertools.count(1)
_email_counter = itertools.count(1)


def _next_name() -> str:
    return f"User {next(_name_counter)}"


def _next_email() -> str:
    return f"user{next(_email_counter)}@test.lan"


def _hashed_password() -> str:
    return hash_password("testpassword123")


class UserFactory(BaseFactory[UserDO]):
    class Meta:
        model = UserDO

    id = Use(uuid.uuid4)
    name = Use(_next_name)
    email = Use(_next_email)
    hashed_password = Use(_hashed_password)
    is_active = True

    @classmethod
    def to_api_response_data(cls, orm_model: UserDO | list[UserDO]) -> dict | list[dict]:
        if isinstance(orm_model, list):
            return [cls._serialize_one(item, UserRead) for item in orm_model]
        return cls._serialize_one(orm_model, UserRead)
