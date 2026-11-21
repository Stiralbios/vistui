"""Hybrid search service: merges ParadeDB BM25 matches with embedding cosine similarity.

For each memory type (message, event, topic, fact) the service gathers up to
`_CANDIDATE_LIMIT` candidates from each retrieval mode, collects both raw scores,
normalizes the channels and applies the chatgroup's ranking formula. The ranked hits
are then packed into the request's token budget, split per memory type by a ratio
that comes from the decision model (chatgroups opting in via `use_llm_memory_ratio`)
or from the chatgroup config (see `backend.search.budget`).
"""

import uuid
from typing import Any

from backend.chat.models import ChatDO
from backend.chatgroup.schemas import ChatGroupInternal
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO, EventMessageLinkDO
from backend.fact.models import FactDO, FactMessageLinkDO
from backend.llm.provider import ChoiceQuestion, DecisionError, Provider
from backend.message.models import MessageDO
from backend.search.budget import (
    MEMORY_TYPES,
    estimate_tokens,
    normalize_probabilities,
    pack_token_budget,
)
from backend.search.schemas import SearchHit, SearchResponse
from backend.search.scoring import clamp_unit, evaluate_ranking_formula, normalize_bm25
from backend.topic.models import TopicDO, TopicMessageLinkDO
from loguru import logger
from paradedb.sqlalchemy.search import parse
from paradedb.sqlalchemy.vector import cosine_distance
from sqlalchemy import Float, func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Candidates fetched per memory type and per retrieval mode before formula ranking.
_CANDIDATE_LIMIT = 30

# Salience used by the formula when the object has no score or the chatgroup ignores salience.
_NEUTRAL_SALIENCE = 1.0

# Decision task resolving the memory-ratio override in providers.yaml, and the name
# of the single choice question asked in the ratio decide() call. The decision model
# is kept loaded between searches so the opt-in costs one call, not one model load.
_MEMORY_RATIO_TASK = "memory_ratio"
_MEMORY_RATIO_QUESTION = "memory_ratio"
_MEMORY_RATIO_KEEP_ALIVE = "10m"


# ParadeDB's query-string parser treats free text as Lucene syntax: an unbalanced quote
# (`a"b`), a field selector (`field:value`), a wildcard (`boots*`) or a bare keyword
# (`NOT`) either raise a parser error (surfacing as a 500) or silently change query
# semantics. Every whitespace-separated token is therefore quoted as a literal phrase;
# the BM25 analyzer still normalizes each token, so matching is unchanged.
def _quote_bm25_tokens(q: str) -> str:
    def quote(token: str) -> str:
        escaped = token.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    return " ".join(quote(token) for token in q.split())


async def search_chatgroup(chatgroup: ChatGroupInternal, q: str, k: int, token_budget: int) -> SearchResponse:
    """Hybrid-search one chatgroup, pack the hits into `token_budget` split by the
    memory ratio, and return the k best packed hits."""
    memory_ratio, ratio_source = await _memory_ratio(chatgroup, q)
    if not q.strip():
        empty_used = dict.fromkeys(MEMORY_TYPES, 0)
        return SearchResponse(
            q=q,
            hits=[],
            token_budget=token_budget,
            tokens_used=empty_used,
            memory_ratio=memory_ratio,
            ratio_source=ratio_source,
        )
    # The semantic channel embeds the raw query; the lexical channel runs it through
    # ParadeDB's query-string parser, which needs the tokens quoted as literals.
    query_vec = (await Provider().embed([q]))[0]
    bm25_q = _quote_bm25_tokens(q)
    async with get_async_sessionmaker()() as session:
        chat_ids = list((await session.scalars(select(ChatDO.id).where(ChatDO.chatgroup_id == chatgroup.id))).all())
        candidates = await _search_messages(session, chatgroup, bm25_q, query_vec, chat_ids)
        candidates += await _search_events(session, chatgroup, bm25_q, query_vec, chat_ids)
        candidates += await _search_topics(session, chatgroup, bm25_q, query_vec)
        candidates += await _search_facts(session, chatgroup, bm25_q, query_vec)

    # Normalize BM25 against the best lexical hit of this query across all memory types,
    # then rank: the channels are only comparable once both live in 0..1.
    bm25_max = max((candidate.bm25_score for candidate in candidates), default=0.0)
    hits = [_rank_hit(chatgroup, candidate, bm25_max) for candidate in candidates]
    packed, tokens_used = pack_token_budget(hits, token_budget, memory_ratio, k)
    return SearchResponse(
        q=q,
        hits=packed,
        token_budget=token_budget,
        tokens_used=tokens_used,
        memory_ratio=memory_ratio,
        ratio_source=ratio_source,
    )


async def _memory_ratio(chatgroup: ChatGroupInternal, q: str) -> tuple[dict[str, float], str]:
    """Resolve the per-type memory ratio for one search and report its source.

    Chatgroups with `use_llm_memory_ratio` true ask the decision model: one decide()
    call whose state carries the chatgroup description and the query, with a single
    choice question whose criteria are the memory types the chatgroup offers (the
    `memory_ratio_criteria` prompts key) — the answer's native probabilities are
    the distribution, renormalized to 1. Any failure (decision error, absent
    probabilities, nothing usable in them) falls back to the config ratio, which is
    used verbatim otherwise (no renormalization).
    """
    config_ratio = chatgroup.config.memory_ratio.model_dump()
    if not chatgroup.config.use_llm_memory_ratio:
        return config_ratio, "config"
    try:
        answers = await Provider().decide(
            {"chatgroup": chatgroup.description, "query": q},
            {
                _MEMORY_RATIO_QUESTION: ChoiceQuestion(
                    instructions=chatgroup.prompts.memory_ratio,
                    criteria=chatgroup.prompts.memory_ratio_criteria,
                )
            },
            task=_MEMORY_RATIO_TASK,
            keep_alive=_MEMORY_RATIO_KEEP_ALIVE,
        )
        probabilities = answers[_MEMORY_RATIO_QUESTION].probabilities
        ratio = normalize_probabilities(probabilities) if probabilities else None
    except DecisionError:
        ratio = None
    if ratio is None:
        logger.warning("memory-ratio decision failed; falling back to the chatgroup config ratio")
        return config_ratio, "config"
    return ratio, "decision"


def _rank_hit(chatgroup: ChatGroupInternal, candidate: SearchHit, bm25_max: float) -> SearchHit:
    """Normalize one candidate's channels and apply the chatgroup's ranking formula."""
    vector_score = clamp_unit(candidate.vector_score)
    bm25_score = normalize_bm25(candidate.bm25_score, bm25_max)
    score = evaluate_ranking_formula(
        chatgroup.ranking_formula, vector_score, bm25_score, candidate.salience, candidate.linked_count
    )
    return candidate.model_copy(update={"score": score, "vector_score": vector_score, "bm25_score": bm25_score})


async def _search_messages(
    session: AsyncSession, chatgroup: ChatGroupInternal, bm25_q: str, query_vec: list[float], chat_ids: list[uuid.UUID]
) -> list[SearchHit]:
    """Search chat messages belonging to the chatgroup's chats."""
    bm25_rows = (
        await session.execute(
            select(MessageDO.id, func.pdb.score(MessageDO.full_text))
            .where(parse(MessageDO.full_text, bm25_q), MessageDO.chat_id.in_(chat_ids))
            .order_by(func.pdb.score(MessageDO.full_text).desc())
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    bm25: dict[uuid.UUID, float] = {row[0]: float(row[1]) for row in bm25_rows}

    vector_rows = (
        await session.execute(
            select(MessageDO.id, cosine_distance(MessageDO.embedding, query_vec).cast(Float))
            .where(MessageDO.embedding.is_not(None), MessageDO.chat_id.in_(chat_ids))
            .order_by(cosine_distance(MessageDO.embedding, query_vec))
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    vector: dict[uuid.UUID, float] = {row[0]: 1.0 - float(row[1]) for row in vector_rows}

    objects = {
        obj.id: obj
        for obj in (await session.scalars(select(MessageDO).where(MessageDO.id.in_(set(bm25) | set(vector))))).all()
    }
    linked = await _message_link_counts(session, list(objects))
    return _build_hits(chatgroup, "message", objects, bm25, vector, linked)


async def _search_events(
    session: AsyncSession, chatgroup: ChatGroupInternal, bm25_q: str, query_vec: list[float], chat_ids: list[uuid.UUID]
) -> list[SearchHit]:
    """Search events belonging to the chatgroup's chats."""
    bm25_rows = (
        await session.execute(
            select(EventDO.id, func.pdb.score(EventDO.full_text))
            .where(parse(EventDO.full_text, bm25_q), EventDO.chat_id.in_(chat_ids))
            .order_by(func.pdb.score(EventDO.full_text).desc())
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    bm25: dict[uuid.UUID, float] = {row[0]: float(row[1]) for row in bm25_rows}

    vector_rows = (
        await session.execute(
            select(EventDO.id, cosine_distance(EventDO.embedding, query_vec).cast(Float))
            .where(EventDO.embedding.is_not(None), EventDO.chat_id.in_(chat_ids))
            .order_by(cosine_distance(EventDO.embedding, query_vec))
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    vector: dict[uuid.UUID, float] = {row[0]: 1.0 - float(row[1]) for row in vector_rows}

    objects = {
        obj.id: obj
        for obj in (await session.scalars(select(EventDO).where(EventDO.id.in_(set(bm25) | set(vector))))).all()
    }
    linked = await _event_link_counts(session, list(objects))
    return _build_hits(chatgroup, "event", objects, bm25, vector, linked)


async def _search_topics(
    session: AsyncSession, chatgroup: ChatGroupInternal, bm25_q: str, query_vec: list[float]
) -> list[SearchHit]:
    """Search topics belonging to the chatgroup."""
    bm25_rows = (
        await session.execute(
            select(TopicDO.id, func.pdb.score(TopicDO.full_text))
            .where(parse(TopicDO.full_text, bm25_q), TopicDO.chatgroup_id == chatgroup.id)
            .order_by(func.pdb.score(TopicDO.full_text).desc())
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    bm25: dict[uuid.UUID, float] = {row[0]: float(row[1]) for row in bm25_rows}

    vector_rows = (
        await session.execute(
            select(TopicDO.id, cosine_distance(TopicDO.embedding, query_vec).cast(Float))
            .where(TopicDO.embedding.is_not(None), TopicDO.chatgroup_id == chatgroup.id)
            .order_by(cosine_distance(TopicDO.embedding, query_vec))
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    vector: dict[uuid.UUID, float] = {row[0]: 1.0 - float(row[1]) for row in vector_rows}

    objects = {
        obj.id: obj
        for obj in (await session.scalars(select(TopicDO).where(TopicDO.id.in_(set(bm25) | set(vector))))).all()
    }
    linked = await _topic_link_counts(session, list(objects))
    return _build_hits(chatgroup, "topic", objects, bm25, vector, linked)


async def _search_facts(
    session: AsyncSession, chatgroup: ChatGroupInternal, bm25_q: str, query_vec: list[float]
) -> list[SearchHit]:
    """Search facts belonging to the chatgroup."""
    bm25_rows = (
        await session.execute(
            select(FactDO.id, func.pdb.score(FactDO.full_text))
            .where(parse(FactDO.full_text, bm25_q), FactDO.chatgroup_id == chatgroup.id)
            .order_by(func.pdb.score(FactDO.full_text).desc())
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    bm25: dict[uuid.UUID, float] = {row[0]: float(row[1]) for row in bm25_rows}

    vector_rows = (
        await session.execute(
            select(FactDO.id, cosine_distance(FactDO.embedding, query_vec).cast(Float))
            .where(FactDO.embedding.is_not(None), FactDO.chatgroup_id == chatgroup.id)
            .order_by(cosine_distance(FactDO.embedding, query_vec))
            .limit(_CANDIDATE_LIMIT)
        )
    ).all()
    vector: dict[uuid.UUID, float] = {row[0]: 1.0 - float(row[1]) for row in vector_rows}

    objects = {
        obj.id: obj
        for obj in (await session.scalars(select(FactDO).where(FactDO.id.in_(set(bm25) | set(vector))))).all()
    }
    linked = await _fact_link_counts(session, list(objects))
    return _build_hits(chatgroup, "fact", objects, bm25, vector, linked)


async def _message_link_counts(session: AsyncSession, message_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Count memory objects linked to each message, summed across event/topic/fact links."""
    counts = dict.fromkeys(message_ids, 0)
    if not message_ids:
        return counts
    columns = (EventMessageLinkDO.message_id, TopicMessageLinkDO.message_id, FactMessageLinkDO.message_id)
    for column in columns:
        rows = (
            await session.execute(select(column, func.count()).where(column.in_(message_ids)).group_by(column))
        ).all()
        for message_id, count in rows:
            counts[message_id] += int(count)
    return counts


async def _event_link_counts(session: AsyncSession, event_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Count messages linked to each event."""
    return await _count_links(session, EventMessageLinkDO.event_id, event_ids)


async def _topic_link_counts(session: AsyncSession, topic_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Count messages linked to each topic."""
    return await _count_links(session, TopicMessageLinkDO.topic_id, topic_ids)


async def _fact_link_counts(session: AsyncSession, fact_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Count messages linked to each fact."""
    return await _count_links(session, FactMessageLinkDO.fact_id, fact_ids)


async def _count_links(session: AsyncSession, link_column: Any, object_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Count links per candidate id for one link-table column."""
    counts = dict.fromkeys(object_ids, 0)
    if not object_ids:
        return counts
    rows = (
        await session.execute(
            select(link_column, func.count()).where(link_column.in_(object_ids)).group_by(link_column)
        )
    ).all()
    for object_id, count in rows:
        counts[object_id] += int(count)
    return counts


def _build_hits(
    chatgroup: ChatGroupInternal,
    type_label: str,
    objects: dict[uuid.UUID, Any],
    bm25_scores: dict[uuid.UUID, float],
    vector_scores: dict[uuid.UUID, float],
    linked_counts: dict[uuid.UUID, int],
) -> list[SearchHit]:
    """Turn raw retrieval scores into candidates; ranking happens in `_rank_hit`.

    Scores here are still raw (unbounded BM25) because BM25 can only be normalized once the
    best lexical hit of the whole query is known, which requires every type's candidates.
    """
    hits = []
    for object_id, obj in objects.items():
        salience = obj.salience_score if obj.salience_score is not None else _NEUTRAL_SALIENCE
        if not chatgroup.config.use_salience:
            salience = _NEUTRAL_SALIENCE
        hits.append(
            SearchHit(
                type=type_label,
                id=object_id,
                text=obj.full_text,
                score=0.0,
                vector_score=vector_scores.get(object_id, 0.0),
                bm25_score=bm25_scores.get(object_id, 0.0),
                salience=float(salience),
                linked_count=linked_counts.get(object_id, 0),
                tokens=estimate_tokens(obj.full_text),
            )
        )
    return hits
