import itertools
import uuid

from backend.chat.models import ChatDO
from backend.chat.schemas import ChatRead
from polyfactory import Use

from tests.backend.config import BaseFactory

_name_counter = itertools.count(1)
_description_counter = itertools.count(1)


def _next_name() -> str:
    return f"Chat {next(_name_counter)}"


def _next_description() -> str:
    return f"Chat description {next(_description_counter)}"


class ChatFactory(BaseFactory[ChatDO]):
    class Meta:
        model = ChatDO

    id = Use(uuid.uuid4)
    name = Use(_next_name)
    description = Use(_next_description)

    @classmethod
    def to_api_response_data(cls, orm_model: ChatDO | list[ChatDO]) -> dict | list[dict]:
        if isinstance(orm_model, list):
            return [cls._serialize_one(item, ChatRead) for item in orm_model]
        return cls._serialize_one(orm_model, ChatRead)
