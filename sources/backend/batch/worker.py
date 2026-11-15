import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from backend.batch.embeddings import embed_memory_objects, embed_messages
from backend.batch.pipelines import EventPipeline, FactPipeline, TopicPipeline
from backend.batch.queries import BatchQueries
from backend.batch.salience import SaliencePipeline, score_memory_objects
from backend.batch.status import batch_status
from backend.database import get_async_sessionmaker
from backend.llm import Provider
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
from backend.settings import AppSettings
from loguru import logger


class BatchWorker:
    """Runs one consolidation round over all pipelines, in dependency order:
    embeddings first, then the event/topic/fact LLM pipelines.

    Progress is mirrored into `batch_status` for the admin API; a failing pipeline marks
    the message it was working on as FAILED without blocking the remaining pipelines.
    """

    def __init__(self) -> None:
        self.should_stop = False
        self.provider: Provider | None = None

    async def get_provider(self) -> Provider:
        """Lazily create and cache the LLM provider shared by all pipelines of this worker."""
        if self.provider is None:
            self.provider = Provider()
        return self.provider

    async def run_round(self) -> None:
        """Run each pipeline exactly once, each in its own transaction, and refresh the
        queue depth before and after so the admin status stays accurate."""
        provider = await self.get_provider()
        settings = AppSettings()

        batch_status.running = True
        batch_status.current_pipeline = None
        batch_status.current_message_id = None

        try:
            async with get_async_sessionmaker()() as session, session.begin():
                batch_status.queue_depth = await BatchQueries.count_waiting_messages(session)

            # (name used for status/logs and as processing_state key, async callable).
            # The per-round work limits come from the VISTUI_BATCH_* settings; the
            # consolidation pipelines event/topic/fact are single-message by design.
            pipelines: list[tuple[str, Callable[..., Awaitable[Any]]]] = [
                ("embedding", partial(embed_messages, limit=settings.VISTUI_BATCH_EMBEDDING_LIMIT)),
                (
                    "memory_embedding",
                    partial(embed_memory_objects, limit=settings.VISTUI_BATCH_MEMORY_EMBEDDING_LIMIT),
                ),
                ("salience", partial(SaliencePipeline.run_round, limit=settings.VISTUI_BATCH_SALIENCE_LIMIT)),
                (
                    "memory_salience",
                    partial(score_memory_objects, limit=settings.VISTUI_BATCH_MEMORY_SALIENCE_LIMIT),
                ),
                ("event", partial(EventPipeline.run_round, block_size=settings.VISTUI_BATCH_EVENT_BLOCK_SIZE)),
                ("topic", TopicPipeline.run_round),
                ("fact", FactPipeline.run_round),
            ]

            for pipeline_name, pipeline in pipelines:
                if self.should_stop:
                    logger.info(f"Batch worker stopping before pipeline {pipeline_name}")
                    break

                batch_status.current_pipeline = pipeline_name

                target_message_id = await self._peek_message_id(pipeline_name)
                batch_status.current_message_id = target_message_id

                try:
                    await pipeline(provider=provider)
                except Exception as exc:
                    batch_status.recent_failures += 1
                    logger.exception(f"Pipeline {pipeline_name} failed: {exc}")
                    if target_message_id:
                        await self._mark_failed(target_message_id, pipeline_name)

            async with get_async_sessionmaker()() as session, session.begin():
                batch_status.queue_depth = await BatchQueries.count_waiting_messages(session)
        finally:
            batch_status.running = False
            batch_status.current_pipeline = None
            batch_status.current_message_id = None

    @staticmethod
    async def _peek_message_id(state_key: str) -> uuid.UUID | None:
        """Return the id of the next message waiting for this stage (None for the batch
        stages — embeddings and both salience stages — which operate on batches rather
        than a single message). The fact stage ignores the protected window like its
        pipeline does."""
        if state_key in ("embedding", "memory_embedding", "salience", "memory_salience"):
            return None
        async with get_async_sessionmaker()() as session, session.begin():
            message = await BatchQueries.get_single_message_with_state(
                session, state_key, outside_protected_window=state_key != "fact"
            )
            return message.id if message else None

    @staticmethod
    async def _mark_failed(message_id: uuid.UUID, state_key: str) -> None:
        """Flag the stage as FAILED on the message and record retry metadata (retry_count,
        last_failure_at) in its processing_state."""
        async with get_async_sessionmaker()() as session, session.begin():
            message = await session.get(MessageDO, message_id)
            if message is None:
                return
            state = dict(message.processing_state)
            state[state_key] = ProcessingStatus.FAILED
            state["retry_count"] = state.get("retry_count", 0) + 1
            state["last_failure_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            message.processing_state = state
