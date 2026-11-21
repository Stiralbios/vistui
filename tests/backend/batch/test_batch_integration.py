"""Integration tests for the batch consolidation against the real Ollama provider.

Unlike test_batch.py, nothing is mocked here: the app is booted through its real
lifespan (which seeds the 30 demo messages on startup, like a normal app start with
VISTUI_SEED_DEMO=1), then real batch worker rounds are triggered through the admin
API and the consolidation results are checked in the database.

These tests are slow (each round runs a real embedding batch, per-element salience
and event-boundary decision calls against the decision model, plus one real LLM call
per consolidated message — several minutes per round with the local models) and need
a reachable provider as configured in settings.yaml. They are opt-in: run them with
`just test-integration` (sets VISTUI_INTEGRATION_TESTS=1).
"""

import os

import pytest
from backend.batch import batch_status
from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO, EventMessageLinkDO
from backend.fact.models import FactDO
from backend.llm.config import get_provider_config
from backend.main import app
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from backend.settings import AppSettings
from backend.topic.models import TopicDO
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("VISTUI_INTEGRATION_TESTS"),
        reason="integration tests are opt-in (run with `just test-integration`)",
    ),
]

# Each round runs one embedding batch plus, per consolidated message, a real salience
# or event-boundary decision call and a real LLM call (event summary, topic or fact
# candidates), so a round takes several minutes with the real providers: keep the
# default low and override with VISTUI_INTEGRATION_MAX_ROUNDS for longer runs.
MAX_ROUNDS = int(os.environ.get("VISTUI_INTEGRATION_MAX_ROUNDS", "2"))
EMBEDDING_DIMENSIONS = get_provider_config().defaults_models.embedding.dimensions
# Per-round work limits of the batch stages, as the worker reads them from the
# settings (overridable through the VISTUI_BATCH_* environment variables).
SALIENCE_PER_ROUND = AppSettings().VISTUI_BATCH_SALIENCE_LIMIT
EVENT_PER_ROUND = AppSettings().VISTUI_BATCH_EVENT_MESSAGE_LIMIT
TOPIC_PER_ROUND = AppSettings().VISTUI_BATCH_TOPIC_MESSAGE_LIMIT
FACT_PER_ROUND = AppSettings().VISTUI_BATCH_FACT_MESSAGE_LIMIT
# A message leaves the queue once every stage has flagged it done, so the queue drains
# at the rate of the slowest stage.
DRAIN_PER_ROUND = min(SALIENCE_PER_ROUND, EVENT_PER_ROUND, TOPIC_PER_ROUND, FACT_PER_ROUND)


async def _queue_depth(client: AsyncClient) -> int:
    response = await client.get("/admin/batch/status")
    assert response.status_code == 200
    return response.json()["queue_depth"]


PROTECTED_COUNT = AppSettings().VISTUI_PROTECTED_MESSAGE_COUNT


def _assert_real_embedding(obj) -> None:
    assert obj.embedding is not None, "embedding should have been computed by the real provider"
    assert len(obj.embedding) == EMBEDDING_DIMENSIONS


async def test_startup_demo_data_real_provider_consolidation(client: AsyncClient) -> None:
    """End-to-end scenario: app startup seeds the 30 demo messages (real data), then
    admin-triggered worker rounds embed and consolidate them using the real provider."""
    failures_before = batch_status.recent_failures

    async with app.router.lifespan_context(app):
        # 1. Startup: the lifespan seeds the demo chat with 30 waiting messages.
        async with get_async_sessionmaker()() as session, session.begin():
            chat = await session.scalar(select(ChatDO).where(ChatDO.name == "Demo Chat"))
            assert chat is not None, "app startup (VISTUI_SEED_DEMO=1) should have seeded the demo chat"
            chatgroup = await session.scalar(select(ChatGroupDO).where(ChatGroupDO.id == chat.chatgroup_id))
            assert chatgroup is not None
            # The demo chatgroup keeps the default protected window
            # (VISTUI_PROTECTED_MESSAGE_COUNT, 10 by default, overridable per chatgroup
            # via its config): the oldest messages are eligible for consolidation, the
            # PROTECTED_COUNT most recent must stay untouched.
            assert chatgroup.config.get("protected_message_count") == PROTECTED_COUNT

        assert await _queue_depth(client) == 30

        # 2. Trigger real worker rounds through the admin API.
        depths = []
        for _ in range(MAX_ROUNDS):
            response = await client.post("/admin/batch/trigger")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            depths.append(await _queue_depth(client))

        # The worker rounds swallow per-message consolidation failures into
        # recent_failures (the failed message keeps its waiting flag, retried by later
        # rounds), so the accounting is exact only when every model answer was
        # well-formed; with a flaky answer the isolation invariants replace it.
        failures = batch_status.recent_failures - failures_before

        if failures == 0:
            # Every stage processes several messages per round, oldest first: the
            # queue drains by DRAIN_PER_ROUND per round while eligible messages remain,
            # then stops at the protected window (those messages keep their waiting
            # flags).
            assert depths == [
                30 - min(round_index * DRAIN_PER_ROUND, 30 - PROTECTED_COUNT)
                for round_index in range(1, MAX_ROUNDS + 1)
            ]
        else:
            # The queue only drains (failed messages stay, nothing re-queues), and the
            # first round consolidated at least one message end to end.
            assert all(later <= earlier for earlier, later in zip(depths, depths[1:]))
            assert depths[0] < 30

        # 3. Check the real consolidation results in the database.
        async with get_async_sessionmaker()() as session, session.begin():
            chat = await session.scalar(select(ChatDO).where(ChatDO.name == "Demo Chat"))
            assert chat is not None
            chat_id = chat.id
            messages = list(
                (
                    await session.execute(
                        select(MessageDO)
                        .where(MessageDO.chat_id == chat_id)
                        .order_by(MessageDO.created_at.asc(), MessageDO.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert len(messages) == 30

            # Embedding stage: all 30 messages embedded in the first round (limit 32 >= 30).
            for message in messages:
                _assert_real_embedding(message)

            # Consolidation failures isolate per message: a stage flag never flips to
            # FAILED (the failed message stays waiting and is retried by later rounds),
            # and every retried message carries its failure metadata.
            for message in messages:
                for flag in ("event", "topic", "fact"):
                    assert message.processing_state[flag] != ProcessingStatus.FAILED
                if message.processing_state.get("retry_count"):
                    assert message.processing_state.get("last_failure_at")

            if failures == 0:
                # Salience stage: the decision model (d3:4b) scored the SALIENCE_PER_ROUND
                # oldest messages per round with a real salience in [0, 1]; like the
                # embedding stage it ignores the protected window, and the rest are still
                # waiting (their salience_score column is still NULL).
                for message in messages[: SALIENCE_PER_ROUND * MAX_ROUNDS]:
                    assert message.salience_score is not None
                    assert 0.0 <= message.salience_score <= 1.0
                for message in messages[SALIENCE_PER_ROUND * MAX_ROUNDS :]:
                    assert message.salience_score is None

                # Event stage: up to EVENT_PER_ROUND non-protected messages per round.
                event_done = [m for m in messages if m.processing_state["event"] == ProcessingStatus.DONE]
                assert len(event_done) == EVENT_PER_ROUND * MAX_ROUNDS

                # Topic and fact stages: up to their own per-round limits each, oldest first.
                for message in messages[: TOPIC_PER_ROUND * MAX_ROUNDS]:
                    assert message.processing_state["topic"] == ProcessingStatus.DONE
                    assert message.processing_state["fact"] == ProcessingStatus.DONE
            else:
                # A stage never exceeds its per-round limits, whatever failed.
                scored = [m for m in messages if m.salience_score is not None]
                assert scored == messages[: len(scored)]  # rolled-back rounds restart from the oldest
                assert len(scored) <= SALIENCE_PER_ROUND * MAX_ROUNDS
                event_done = [m for m in messages if m.processing_state["event"] == ProcessingStatus.DONE]
                assert 1 <= len(event_done) <= EVENT_PER_ROUND * MAX_ROUNDS
                topic_done = [m for m in messages if m.processing_state["topic"] == ProcessingStatus.DONE]
                fact_done = [m for m in messages if m.processing_state["fact"] == ProcessingStatus.DONE]
                assert 1 <= len(topic_done) <= TOPIC_PER_ROUND * MAX_ROUNDS
                assert 1 <= len(fact_done) <= FACT_PER_ROUND * MAX_ROUNDS
                for message in event_done + topic_done + fact_done:
                    assert message not in messages[-PROTECTED_COUNT:]

            # Protected window: the PROTECTED_COUNT most recent messages keep their
            # event/topic/fact flags waiting (protected messages are never added to
            # facts/events/topics; they become eligible only as the chat grows)...
            for message in messages[-PROTECTED_COUNT:]:
                assert message.processing_state["event"] == ProcessingStatus.WAITING
                assert message.processing_state["topic"] == ProcessingStatus.WAITING
                assert message.processing_state["fact"] == ProcessingStatus.WAITING
                # ...but the embedding stage ignores the window (a NULL embedding
                # would mean still queued).
                assert message.embedding is not None

            # Events: the first consolidated message forces a new event; later rounds may
            # create more or only extend the last one, each linked to at least one source
            # message, with a coherent time range.
            events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat_id))).scalars().all())
            assert len(events) >= 1
            for event in events:
                assert event.start_at <= event.end_at
            links = list(
                (
                    await session.execute(
                        select(EventMessageLinkDO).where(EventMessageLinkDO.event_id.in_([e.id for e in events]))
                    )
                )
                .scalars()
                .all()
            )
            assert len(links) >= len(events)

            # Topics and facts: created by the real LLM for the processed messages.
            topics = list(
                (await session.execute(select(TopicDO).where(TopicDO.chatgroup_id == chat.chatgroup_id)))
                .scalars()
                .all()
            )
            facts = list(
                (await session.execute(select(FactDO).where(FactDO.chatgroup_id == chat.chatgroup_id))).scalars().all()
            )
            assert len(topics) >= 1
            assert len(facts) >= 1

            # Memory objects created in an earlier round are re-embedded by the next one:
            # the memory stages run before the event stage within a round, so an event
            # that the final round did not rewrite keeps the embedding it got then.
            # Rewritten events (extend/mix) legitimately wait for the next round, so
            # this is only asserted as "at least one embedded event". Topics and facts
            # are created whenever the LLM decides to, possibly after the memory stage
            # already ran, so their re-embedding is not asserted here.
            if MAX_ROUNDS >= 2:
                assert any(
                    event.embedding is not None and len(event.embedding) == EMBEDDING_DIMENSIONS for event in events
                )
                # Likewise, the memory salience stage of a later round scored the
                # events created by an earlier one (same rewrite caveat).
                assert any(event.salience_score is not None and 0.0 <= event.salience_score <= 1.0 for event in events)

        # 4. Hybrid search through the real API: real query embedding + ParadeDB BM25.
        login = await client.post(
            "/api/v1/auth/jwt/login",
            data={"username": "test@example.com", "password": "devpassword123"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        response = await client.get(
            f"/api/v1/chatgroups/{chat.chatgroup_id}/search",
            params={"q": "hiking Japan photography"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        hits = data["hits"]
        assert len(hits) > 0
        scores = [hit["score"] for hit in hits]
        assert scores == sorted(scores, reverse=True)
        # Real BM25 matched the demo conversation, and the real vector similarity
        # retrieved the embedded memories; both channels are merged in the hits.
        assert any(hit["type"] == "message" and hit["bm25_score"] > 0 for hit in hits)
        assert any(hit["vector_score"] > 0 for hit in hits)
        # The chatgroup has not opted into the decision-model ratio: the config ratio
        # packs the (generous) default budget.
        assert data["ratio_source"] == "config"
        assert data["token_budget"] == 2000

        # 5. Token-budget packing with the real decision-model ratio: opt the demo
        # chatgroup into `use_llm_memory_ratio` and search with a small budget. The
        # real d3 call must return a usable distribution over the four types; the
        # packed hits respect it, with the best-hit rule as the only overshoot.
        async with get_async_sessionmaker()() as session, session.begin():
            chatgroup = await session.scalar(select(ChatGroupDO).where(ChatGroupDO.id == chat.chatgroup_id))
            chatgroup.config = {**chatgroup.config, "use_llm_memory_ratio": True}

        response = await client.get(
            f"/api/v1/chatgroups/{chat.chatgroup_id}/search",
            params={"q": "hiking Japan photography", "token_budget": 120},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ratio_source"] == "decision"
        assert data["token_budget"] == 120
        ratio = data["memory_ratio"]
        assert set(ratio) == {"message", "event", "topic", "fact"}
        assert sum(ratio.values()) == pytest.approx(1.0)
        hits = data["hits"]
        assert len(hits) <= 20
        scores = [hit["score"] for hit in hits]
        assert scores == sorted(scores, reverse=True)
        assert all(hit["tokens"] > 0 for hit in hits)
        for label, used in data["tokens_used"].items():
            assert used == sum(hit["tokens"] for hit in hits if hit["type"] == label)
            if used > ratio[label] * data["token_budget"]:
                # Only the best-hit rule lets a type exceed its share, and then the
                # type's single best hit is everything it got.
                assert len([hit for hit in hits if hit["type"] == label]) == 1
