import itertools
import uuid

from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import DEFAULT_RANKING_FORMULA, ChatGroupConfig, ChatGroupPrompts, ChatGroupRead
from polyfactory import Use

from tests.backend.config import BaseFactory

_counter = itertools.count(1)


def _next_name() -> str:
    return f"ChatGroup {next(_counter)}"


def _next_description() -> str:
    return f"Description {next(_counter)}"


class ChatGroupFactory(BaseFactory[ChatGroupDO]):
    class Meta:
        model = ChatGroupDO

    id = Use(uuid.uuid4)
    name = Use(_next_name)
    description = Use(_next_description)
    config = ChatGroupConfig().model_dump()
    prompts = ChatGroupPrompts().model_dump()
    ranking_formula = DEFAULT_RANKING_FORMULA

    @classmethod
    def to_api_response_data(cls, orm_model: ChatGroupDO | list[ChatGroupDO]) -> dict | list[dict]:
        if isinstance(orm_model, list):
            return [cls._serialize_one(item, ChatGroupRead) for item in orm_model]
        return cls._serialize_one(orm_model, ChatGroupRead)
