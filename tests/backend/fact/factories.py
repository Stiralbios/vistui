import datetime as dt
import itertools
import uuid

from backend.fact.models import FactDO
from backend.fact.schemas import FactRead
from polyfactory import Use

from tests.backend.config import BaseFactory

_summary_counter = itertools.count(1)


def _next_summary() -> str:
    return f"Fact summary {next(_summary_counter)}"


def _now() -> dt.datetime:
    return dt.datetime.now()


class FactFactory(BaseFactory[FactDO]):
    class Meta:
        model = FactDO

    id = Use(uuid.uuid4)
    full_text = Use(_next_summary)
    start_at = Use(_now)
    last_edited_at = Use(_now)
    salience_score = 0.7

    @classmethod
    def to_api_response_data(cls, orm_model: FactDO | list[FactDO]) -> dict | list[dict]:
        if isinstance(orm_model, list):
            return [cls._serialize_one(item, FactRead) for item in orm_model]
        return cls._serialize_one(orm_model, FactRead)
