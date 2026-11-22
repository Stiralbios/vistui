from backend.admin.schemas import BatchStatusResponse, ReembedResponse, SeedDemoResponse, TriggerBatchResponse
from backend.batch import BatchWorker, batch_status
from backend.batch.queries import BatchQueries
from backend.chat.models import ChatDO
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO
from backend.fact.models import FactDO
from backend.message.models import MessageDO
from backend.seeders import seed_demo_chat
from backend.topic.models import TopicDO
from backend.utils.embeddings import embed_texts
from fastapi import APIRouter
from loguru import logger
from sqlalchemy import select

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/seed/demo", response_model=SeedDemoResponse)
async def seed_demo() -> SeedDemoResponse:
    result = await seed_demo_chat()
    return SeedDemoResponse(**result)


@router.post("/batch/trigger", response_model=TriggerBatchResponse)
async def trigger_batch() -> TriggerBatchResponse:
    """Run one batch worker round manually. If a round is already in progress (scheduled
    or another manual trigger), return immediately without starting a second one."""
    worker = BatchWorker()
    if worker.running:
        logger.info("Manual batch trigger ignored: a batch round is already running")
        return TriggerBatchResponse(status="already_running")
    await worker.run_round()
    return TriggerBatchResponse(status="ok")


@router.post("/reembed", response_model=ReembedResponse)
async def reembed() -> ReembedResponse:
    """Recompute embeddings for every message and memory object.

    Used after switching embedding models: vectors from different models are not
    comparable, so everything must be embedded with the (new) model. Embeddings are
    computed synchronously in this endpoint because there is no separate embedding
    batch stage anymore.
    """
    counts: dict[str, int] = {"messages": 0, "events": 0, "topics": 0, "facts": 0}

    async with get_async_sessionmaker()() as session, session.begin():
        messages = list((await session.execute(select(MessageDO))).scalars().all())
        if messages:
            embeddings = await embed_texts([message.full_text for message in messages])
            for message, vector in zip(messages, embeddings):
                message.embedding = vector
        counts["messages"] = len(messages)

    for key, model in (("events", EventDO), ("topics", TopicDO), ("facts", FactDO)):
        async with get_async_sessionmaker()() as session, session.begin():
            objects = list((await session.execute(select(model))).scalars().all())
            if objects:
                embeddings = await embed_texts([obj.full_text for obj in objects])
                for obj, vector in zip(objects, embeddings):
                    obj.embedding = vector
            counts[key] = len(objects)

    return ReembedResponse(**counts)


@router.get("/batch/status", response_model=BatchStatusResponse)
async def get_batch_status() -> BatchStatusResponse:
    queue_depth = 0
    current_chatgroup_id = batch_status.current_chatgroup_id

    async with get_async_sessionmaker()() as session, session.begin():
        queue_depth = await BatchQueries.count_waiting_messages(session)
        if batch_status.current_message_id and current_chatgroup_id is None:
            message = await session.get(MessageDO, batch_status.current_message_id)
            if message is not None:
                chat = await session.get(ChatDO, message.chat_id)
                if chat is not None:
                    current_chatgroup_id = chat.chatgroup_id

    return BatchStatusResponse(
        running=batch_status.running,
        current_pipeline=batch_status.current_pipeline,
        current_message_id=batch_status.current_message_id,
        current_chatgroup_id=current_chatgroup_id,
        queue_depth=queue_depth,
        recent_failures=batch_status.recent_failures,
    )
