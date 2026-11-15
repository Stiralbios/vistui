"""Salience scoring for messages and memory objects with the decision model.

Salience (0.0-1.0) expresses how much a text is worth remembering. It is computed by
the decision model (an ollama System One model such as ``d3:4b``, called through
``Provider.decide``) as a `score` question against a fixed rubric: the answer's
probability-weighted rubric index is normalized by the number of levels minus one.

Every element is scored in its own decide() call, element per element like the other
batch stages: the state carries the chatgroup description plus the single message or
memory object, and one question named "salience".

ChatGroups with ``use_salience`` false or null opt out: their rows keep
``salience_score = NULL`` (nothing is fabricated) and the search formula passes the
neutral 1.0 for them (see ``backend.search.service``).
"""

import uuid
from typing import Protocol, TypeVar

from backend.batch.queries import BatchQueries
from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupConfig, ChatGroupPrompts
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO
from backend.fact.models import FactDO
from backend.llm.provider import DecisionAnswer, Provider, ScoreQuestion
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from backend.search.scoring import clamp_unit
from backend.topic.models import TopicDO
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Ordered rubric for the decision model's `score` question, lowest level first: the
# answer's score (probability-weighted zero-based index) is normalized by len - 1.
SALIENCE_RUBRIC = [
    "Trivial: greetings, filler, or noise; not worth remembering.",
    "Low: minor context that will rarely matter later.",
    "Moderate: useful context or conversational continuity.",
    "High: meaningful personal facts, plans, or preferences worth keeping.",
    "Critical: core information - decisions, key people, dates, commitments.",
]

# Keep the decision model loaded between the per-element calls of a round.
KEEP_ALIVE = "10m"

# Decision task name, resolved against the providers.yaml overrides by
# Provider.decide (falling back to defaults.decision).
SALIENCE_TASK = "salience"

# Name of the single score question asked in each decide() call.
QUESTION_NAME = "salience"


class _Salient(Protocol):
    """Structural type of what the salience stages can score."""

    id: uuid.UUID
    full_text: str
    salience_score: float | None


S = TypeVar("S", bound=_Salient)


def _use_salience(chatgroup: ChatGroupDO) -> bool:
    """False or null `use_salience` opts the chatgroup out of salience scoring."""
    return bool(chatgroup.config.get("use_salience", ChatGroupConfig().use_salience))


def _chatgroup_prompt(chatgroup: ChatGroupDO) -> str:
    """The chatgroup's salience prompt template, used as the question instructions base."""
    return chatgroup.prompts.get("salience") or ChatGroupPrompts().salience


def _question(prompt: str, kind: str) -> ScoreQuestion:
    """Build the score question for the single element of the state."""
    return ScoreQuestion(
        instructions=f"{prompt} Judge the {kind} in the state.",
        criteria=SALIENCE_RUBRIC,
    )


def _normalize(answer: DecisionAnswer) -> float:
    """Map a rubric score answer (0-based weighted index) to a salience in [0, 1]."""
    if answer.score is None:
        raise ValueError(f"salience decision answer has no score: {answer}")
    return clamp_unit(answer.score / (len(SALIENCE_RUBRIC) - 1))


async def _chatgroup_ids_by_chat(
    session: AsyncSession,
    chat_ids: set[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """Map chat id -> chatgroup id for the given chats."""
    if not chat_ids:
        return {}
    res = await session.execute(select(ChatDO).where(ChatDO.id.in_(chat_ids)))
    return {chat.id: chat.chatgroup_id for chat in res.scalars().all()}


async def _load_chatgroups(
    session: AsyncSession,
    chatgroup_ids: set[uuid.UUID],
) -> dict[uuid.UUID, ChatGroupDO]:
    if not chatgroup_ids:
        return {}
    res = await session.execute(select(ChatGroupDO).where(ChatGroupDO.id.in_(chatgroup_ids)))
    return {chatgroup.id: chatgroup for chatgroup in res.scalars().all()}


async def _score_element(
    provider: Provider,
    chatgroup: ChatGroupDO,
    kind: str,
    fields: dict[str, str],
) -> float:
    """Score one element with the decision model: a single decide() call whose state
    carries the chatgroup description plus the element itself. Returns the salience in
    [0, 1]."""
    state = {"chatgroup": chatgroup.description, kind: fields}
    answers = await provider.decide(
        state,
        {QUESTION_NAME: _question(_chatgroup_prompt(chatgroup), kind)},
        task=SALIENCE_TASK,
        keep_alive=KEEP_ALIVE,
    )
    return _normalize(answers[QUESTION_NAME])


class SaliencePipeline:
    """Score the oldest waiting messages with the decision model, one decide() call per
    message.

    The stage runs for every message, protected window included (like the embedding
    stage). Opted-out chatgroups only get their `salience` flag marked done: the
    score stays NULL and search passes the neutral salience.
    """

    @classmethod
    async def run_round(cls, provider: Provider | None = None, limit: int = 10) -> int:
        """Score up to `limit` oldest messages with processing_state.salience == waiting.
        Returns the number of messages handled (scored or opted out)."""
        provider = provider or Provider()
        async with get_async_sessionmaker()() as session, session.begin():
            messages = await BatchQueries.get_messages_with_state(session, "salience", limit)
            if not messages:
                return 0

            chat_to_group = await _chatgroup_ids_by_chat(session, {m.chat_id for m in messages})
            chatgroups = await _load_chatgroups(session, set(chat_to_group.values()))

            by_group: dict[uuid.UUID, list[MessageDO]] = {}
            for message in messages:
                by_group.setdefault(chat_to_group[message.chat_id], []).append(message)

            total = 0
            for chatgroup_id, group_messages in by_group.items():
                chatgroup = chatgroups[chatgroup_id]
                if not _use_salience(chatgroup):
                    for message in group_messages:
                        message.processing_state = {
                            **message.processing_state,
                            "salience": ProcessingStatus.DONE,
                        }
                    total += len(group_messages)
                    continue

                for message in group_messages:
                    message.salience_score = await _score_element(
                        provider,
                        chatgroup,
                        "message",
                        {"role": message.role, "text": message.full_text},
                    )
                    message.processing_state = {**message.processing_state, "salience": ProcessingStatus.DONE}
                total += len(group_messages)

            logger.info(f"Salience pipeline handled {total} message(s)")
            return total


async def _score_objects(
    provider: Provider,
    session: AsyncSession,
    objects: list[S],
    group_ids: dict[uuid.UUID, uuid.UUID],
    kind: str,
) -> int:
    """Score NULL-salience memory objects of one model, one decide() call per object.
    Objects of opted-out chatgroups are skipped and keep their NULL score."""
    if not objects:
        return 0

    chatgroups = await _load_chatgroups(session, set(group_ids.values()))
    by_group: dict[uuid.UUID, list[S]] = {}
    for obj in objects:
        by_group.setdefault(group_ids[obj.id], []).append(obj)

    scored = 0
    for chatgroup_id, group_objects in by_group.items():
        chatgroup = chatgroups[chatgroup_id]
        if not _use_salience(chatgroup):
            continue
        for obj in group_objects:
            obj.salience_score = await _score_element(provider, chatgroup, kind, {"text": obj.full_text})
        scored += len(group_objects)

    if scored:
        logger.info(f"Scored {scored} {kind}(s)")
    return scored


async def score_memory_objects(provider: Provider | None = None, limit: int = 32) -> int:
    """Score events, topics and facts whose salience_score is NULL, one transaction per
    model. Returns the number of objects scored (opted-out chatgroups don't count)."""
    provider = provider or Provider()
    total = 0

    async with get_async_sessionmaker()() as session, session.begin():
        events = await BatchQueries.get_memory_objects_without_salience(session, EventDO, limit)
        if events:
            chat_to_group = await _chatgroup_ids_by_chat(session, {event.chat_id for event in events})
            total += await _score_objects(
                provider,
                session,
                events,
                {event.id: chat_to_group[event.chat_id] for event in events},
                "event",
            )

    async with get_async_sessionmaker()() as session, session.begin():
        topics = await BatchQueries.get_memory_objects_without_salience(session, TopicDO, limit)
        if topics:
            total += await _score_objects(
                provider,
                session,
                topics,
                {topic.id: topic.chatgroup_id for topic in topics},
                "topic",
            )

    async with get_async_sessionmaker()() as session, session.begin():
        facts = await BatchQueries.get_memory_objects_without_salience(session, FactDO, limit)
        if facts:
            total += await _score_objects(
                provider,
                session,
                facts,
                {fact.id: fact.chatgroup_id for fact in facts},
                "fact",
            )

    return total
