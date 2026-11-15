import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from backend.batch.embeddings import embed_memory_objects, embed_messages
from backend.batch.pipelines import EventPipeline, FactPipeline, TopicPipeline
from backend.batch.queries import BatchQueries
from backend.batch.status import batch_status
from backend.database import get_async_sessionmaker
from backend.llm import Provider
from backend.message.constants import ProcessingStatus
from backend.message.models import MessageDO
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

        batch_status.running = True
        batch_status.current_pipeline = None
        batch_status.current_message_id = None

        try:
            async with get_async_sessionmaker()() as session, session.begin():
                batch_status.queue_depth = await BatchQueries.count_waiting_messages(session)

            # (name used for status/logs, processing_state key to peek/mark, async callable)
            pipelines: list[tuple[str, str, Callable[..., Awaitable[Any]]]] = [
                ("embedding", "embedding", embed_messages),
                ("memory_embedding", "memory_embedding", embed_memory_objects),
                ("event", "event", EventPipeline.run_round),
                ("topic", "topic", TopicPipeline.run_round),
                ("fact", "fact", FactPipeline.run_round),
            ]

            for pipeline_name, state_key, pipeline in pipelines:
                if self.should_stop:
                    logger.info(f"Batch worker stopping before pipeline {pipeline_name}")
                    break

                batch_status.current_pipeline = pipeline_name

                target_message_id = await self._peek_message_id(state_key)
                batch_status.current_message_id = target_message_id

                try:
                    if pipeline_name in ("embedding", "memory_embedding"):
                        await pipeline(provider=provider)
                    else:
                        await pipeline(provider=provider)
                except Exception as exc:
                    batch_status.recent_failures += 1
                    logger.exception(f"Pipeline {pipeline_name} failed: {exc}")
                    if target_message_id:
                        await self._mark_failed(target_message_id, state_key)

            async with get_async_sessionmaker()() as session, session.begin():
                batch_status.queue_depth = await BatchQueries.count_waiting_messages(session)
        finally:
            batch_status.running = False
            batch_status.current_pipeline = None
            batch_status.current_message_id = None

    @staticmethod
    async def _peek_message_id(state_key: str) -> uuid.UUID | None:
        """Return the id of the next message waiting for this stage (None for the embedding
        stages, which operate on batches rather than a single message)."""
        if state_key in ("embedding", "memory_embedding"):
            return None
        async with get_async_sessionmaker()() as session, session.begin():
            message = await BatchQueries.get_single_message_with_state(session, state_key)
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
