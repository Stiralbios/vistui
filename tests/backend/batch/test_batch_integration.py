"""Integration tests for the batch consolidation against the real Ollama provider.

Unlike test_batch.py, nothing is mocked here: the app is booted through its real
lifespan (which seeds the 30 demo messages on startup, like a normal app start with
VISTUI_SEED_DEMO=1), then real batch worker rounds are triggered through the admin
API and the consolidation results are checked in the database.

These tests are slow (each LLM call takes tens of seconds with the local Ollama
models) and need a reachable provider as configured in providers.yaml. They are
opt-in: run them with `just test-integration` (sets VISTUI_INTEGRATION_TESTS=1).
"""

import os

import pytest
from backend.batch import batch_status
from backend.chat.models import ChatDO
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO, EventMessageLinkDO
from backend.fact.models import FactDO
from backend.llm.config import get_provider_config
from backend.main import app
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
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

# Each round runs one embedding batch plus one real LLM call per pipeline (event, topic,
# fact), so a round takes minutes with the real provider: keep the default low and
# override with VISTUI_INTEGRATION_MAX_ROUNDS for longer runs.
MAX_ROUNDS = int(os.environ.get("VISTUI_INTEGRATION_MAX_ROUNDS", "2"))
EMBEDDING_DIMENSIONS = get_provider_config().defaults.embedding.dimensions


async def _queue_depth(client: AsyncClient) -> int:
    response = await client.get("/admin/batch/status")
    assert response.status_code == 200
    return response.json()["queue_depth"]


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

        assert await _queue_depth(client) == 30

        # 2. Trigger real worker rounds through the admin API.
        depths = []
        for _ in range(MAX_ROUNDS):
            response = await client.post("/admin/batch/trigger")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            depths.append(await _queue_depth(client))

        # The embedding and event stages are batched, while the topic and fact stages each
        # process one message per round: the queue drains by exactly one message per round.
        assert depths == [30 - i for i in range(1, MAX_ROUNDS + 1)]

        # The worker swallows pipeline errors into recent_failures: none should have fired.
        assert batch_status.recent_failures == failures_before

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
                assert message.processing_state["embedding"] == ProcessingStatus.DONE

            # Event stage: one block of 10 messages per round.
            event_done = [m for m in messages if m.processing_state["event"] == ProcessingStatus.DONE]
            assert len(event_done) == 10 * MAX_ROUNDS

            # Topic and fact stages: the oldest message per round each.
            for message in messages[:MAX_ROUNDS]:
                assert message.processing_state["topic"] == ProcessingStatus.DONE
                assert message.processing_state["fact"] == ProcessingStatus.DONE

            # Events: at least one per round (the pipeline falls back to a single group),
            # each linked to at least one source message, with a coherent time range.
            events = list((await session.execute(select(EventDO).where(EventDO.chat_id == chat_id))).scalars().all())
            assert len(events) >= MAX_ROUNDS
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

            # Memory objects created in an earlier round are re-embedded by the next one.
            # This is only guaranteed for events (the pipeline always creates at least one
            # per round via its fallback group): topics and facts are created whenever the
            # LLM decides to, possibly after the memory stage already ran, so their
            # re-embedding is not asserted here.
            if MAX_ROUNDS >= 2:
                assert any(
                    event.embedding is not None and len(event.embedding) == EMBEDDING_DIMENSIONS for event in events
                )
