import datetime as dt
import itertools
import uuid

from backend.topic.models import TopicDO
from backend.topic.schemas import TopicRead
from polyfactory import Use

from tests.backend.config import BaseFactory

_summary_counter = itertools.count(1)


def _next_summary() -> str:
    return f"Topic summary {next(_summary_counter)}"


def _now() -> dt.datetime:
    return dt.datetime.now()


class TopicFactory(BaseFactory[TopicDO]):
    class Meta:
        model = TopicDO

    id = Use(uuid.uuid4)
    full_text = Use(_next_summary)
    start_at = Use(_now)
    last_edited_at = Use(_now)
    salience_score = 0.7

    @classmethod
    def to_api_response_data(cls, orm_model: TopicDO | list[TopicDO]) -> dict | list[dict]:
        if isinstance(orm_model, list):
            return [cls._serialize_one(item, TopicRead) for item in orm_model]
        return cls._serialize_one(orm_model, TopicRead)
