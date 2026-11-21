"""Salience scoring for messages and memory objects with the decision model.

Salience (0.0-1.0) expresses how much a text is worth remembering. It is computed by
the decision model (an ollama System One model such as ``d3:4b``, called through
``Provider.decide``) as a `score` question against the chatgroup's rubric (2-10
ordered levels, the ``salience_rubric`` prompts key — 5 levels by default): the
answer's probability-weighted rubric index is normalized by the number of levels
minus one, so any rubric length maps to the same 0..1 salience.

Every element is scored in its own decide() call, element per element like the other
batch stages: the state carries the chatgroup description plus the single message or
memory object, and one question named "salience". The question's instructions are
the chatgroup's ``salience`` prompt template with ``{kind}`` substituted.

ChatGroups with ``use_salience`` false opt out: their rows keep
``salience_score = NULL`` (nothing is fabricated) and the search formula passes the
neutral 1.0 for them (see ``backend.search.service``). For messages the opt-out is
enforced in the selection query (no row is ever picked, so the queue never clogs);
for memory objects the stage skips them in Python.
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
from backend.llm.prompts import DEFAULT_SALIENCE_RUBRIC
from backend.llm.provider import DecisionAnswer, Provider, ScoreQuestion
from backend.search.scoring import clamp_unit
from backend.topic.models import TopicDO
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Keep the decision model loaded between the per-element calls of a round.
KEEP_ALIVE = "10m"

# Decision task name, resolved against the settings.yaml overrides by
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
    """A false `use_salience` opts the chatgroup out of salience scoring."""
    return bool(chatgroup.config.get("use_salience", ChatGroupConfig().use_salience))


def _chatgroup_prompt(chatgroup: ChatGroupDO) -> str:
    """The chatgroup's salience instruction template."""
    return chatgroup.prompts.get("salience") or ChatGroupPrompts().salience


def _chatgroup_rubric(chatgroup: ChatGroupDO) -> list[str]:
    """The chatgroup's ordered salience rubric levels, lowest first."""
    return list(chatgroup.prompts.get("salience_rubric") or DEFAULT_SALIENCE_RUBRIC)


def _question(chatgroup: ChatGroupDO, kind: str) -> ScoreQuestion:
    """Build the score question for the single element of the state: the chatgroup's
    instruction template with `{kind}` substituted (a template without the placeholder
    is sent verbatim) and its rubric as the criteria."""
    return ScoreQuestion(
        instructions=_chatgroup_prompt(chatgroup).replace("{kind}", kind),
        criteria=_chatgroup_rubric(chatgroup),
    )


def _normalize(answer: DecisionAnswer, rubric_length: int) -> float:
    """Map a rubric score answer (0-based weighted index) to a salience in [0, 1],
    scaled by the rubric length."""
    if answer.score is None:
        raise ValueError(f"salience decision answer has no score: {answer}")
    return clamp_unit(answer.score / (rubric_length - 1))


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
    question = _question(chatgroup, kind)
    answers = await provider.decide(
        state,
        {QUESTION_NAME: question},
        task=SALIENCE_TASK,
        keep_alive=KEEP_ALIVE,
    )
    return _normalize(answers[QUESTION_NAME], len(question.criteria))


class SaliencePipeline:
    """Score the oldest messages with a NULL salience_score, one decide() call
    per message (the NULL column is the queue marker — a failed round rolls back and
    retries come for free).

    The stage runs for every selected message, protected window included (like the
    embedding stage), and does not wait for the embedding stage. ChatGroups with
    `use_salience` false are excluded by the selection query: their messages keep the
    NULL score (nothing is fabricated) and search passes the neutral salience for them.
    """

    @classmethod
    async def run_round(cls, provider: Provider | None = None, limit: int = 5) -> int:
        """Score up to `limit` oldest messages whose salience_score is NULL
        (the worker passes its configured VISTUI_BATCH_SALIENCE_LIMIT).
        Returns the number of messages scored."""
        provider = provider or Provider()
        async with get_async_sessionmaker()() as session, session.begin():
            messages = await BatchQueries.get_messages_waiting_for_salience(session, limit)
            if not messages:
                return 0

            chat_to_group = await _chatgroup_ids_by_chat(session, {m.chat_id for m in messages})
            chatgroups = await _load_chatgroups(session, set(chat_to_group.values()))

            total = 0
            for message in messages:
                chatgroup = chatgroups[chat_to_group[message.chat_id]]
                message.salience_score = await _score_element(
                    provider,
                    chatgroup,
                    "message",
                    {"role": message.role, "text": message.full_text},
                )
                total += 1

            logger.info(f"Salience pipeline scored {total} message(s)")
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
