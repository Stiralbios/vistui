import datetime as dt
import itertools
import uuid

from backend.event.models import EventDO
from backend.event.schemas import EventRead
from polyfactory import Use

from tests.backend.config import BaseFactory

_summary_counter = itertools.count(1)


def _next_summary() -> str:
    return f"Event summary {next(_summary_counter)}"


def _now() -> dt.datetime:
    return dt.datetime.now()


class EventFactory(BaseFactory[EventDO]):
    class Meta:
        model = EventDO

    id = Use(uuid.uuid4)
    full_text = Use(_next_summary)
    start_at = Use(_now)
    end_at = Use(_now)
    salience_score = 0.7

    @classmethod
    def to_api_response_data(cls, orm_model: EventDO | list[EventDO]) -> dict | list[dict]:
        if isinstance(orm_model, list):
            return [cls._serialize_one(item, EventRead) for item in orm_model]
        return cls._serialize_one(orm_model, EventRead)
