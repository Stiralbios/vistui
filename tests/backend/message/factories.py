import itertools
import uuid

from backend.message.constants import MessageRole
from backend.message.models import MessageDO
from backend.message.schemas import MessageRead
from polyfactory import Use

from tests.backend.config import BaseFactory

_text_counter = itertools.count(1)


def _next_text() -> str:
    return f"Message text {next(_text_counter)}"


class MessageFactory(BaseFactory[MessageDO]):
    class Meta:
        model = MessageDO

    id = Use(uuid.uuid4)
    full_text = Use(_next_text)
    role = MessageRole.USER
    salience_score = 0.5
    processing_state = {
        "embedding": "waiting",
        "salience": "waiting",
        "event": "waiting",
        "topic": "waiting",
        "fact": "waiting",
        "last_failure_at": None,
        "retry_count": 0,
    }

    @classmethod
    def to_api_response_data(cls, orm_model: MessageDO | list[MessageDO]) -> dict | list[dict]:
        if isinstance(orm_model, list):
            return [cls._serialize_one(item, MessageRead) for item in orm_model]
        return cls._serialize_one(orm_model, MessageRead)
