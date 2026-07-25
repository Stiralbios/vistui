import itertools
import uuid

from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupRead
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
    config = {
        "token_budget": 2000,
        "memory_ratio": {"message": 0.40, "event": 0.10, "topic": 0.30, "fact": 0.20},
        "use_llm_memory_ratio": False,
        "use_salience": True,
        "protected_message_count": 10,
        "topic_update_threshold": 0.75,
        "fact_update_threshold": 0.80,
        "fact_history_count": 5,
        "max_facts_per_message": 5,
        "max_topics_per_message": 5,
        "batch_inactivity_seconds": 3600,
        "processing_timeout_seconds": 3600,
    }
    prompts = {
        "salience": "Assign a salience score between 0 and 1 to the message.",
        "keywords": "Extract a list of search keywords from the message.",
        "memory_ratio": "Determine the desired memory category distribution.",
        "event": "Summarize a contiguous block of messages into one or more events.",
        "topic_candidate": "Select existing topics to update or decide to create a new topic.",
        "topic_update": "Update a specific topic given the message and ChatGroup context.",
        "fact_candidate": "Select existing facts to update or decide to create a new fact.",
        "fact_update": "Update a specific fact given the message and its recent history.",
    }
    ranking_formula = (
        "lambda vector_score, bm25_score, salience, linked_count: "
        "max(vector_score * salience * linked_count, bm25_score * salience * linked_count)"
    )

    @classmethod
    def to_api_response_data(cls, orm_model: ChatGroupDO | list[ChatGroupDO]) -> dict | list[dict]:
        if isinstance(orm_model, list):
            return [cls._serialize_one(item, ChatGroupRead) for item in orm_model]
        return cls._serialize_one(orm_model, ChatGroupRead)
