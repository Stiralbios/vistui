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
from fastapi import APIRouter
from sqlalchemy import update

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/seed/demo", response_model=SeedDemoResponse)
async def seed_demo() -> SeedDemoResponse:
    result = await seed_demo_chat()
    return SeedDemoResponse(**result)


@router.post("/batch/trigger", response_model=TriggerBatchResponse)
async def trigger_batch() -> TriggerBatchResponse:
    worker = BatchWorker()
    await worker.run_round()
    return TriggerBatchResponse(status="ok")


@router.post("/reembed", response_model=ReembedResponse)
async def reembed() -> ReembedResponse:
    """Null all embeddings and requeue them. Used after switching embedding models:
    vectors from different models are not comparable, so everything must be recomputed
    by the batch worker: a NULL embedding is the requeue marker itself."""
    async with get_async_sessionmaker()() as session, session.begin():
        messages = (await session.execute(update(MessageDO).values(embedding=None))).rowcount
        counts = {"messages": messages}
        for key, model in (("events", EventDO), ("topics", TopicDO), ("facts", FactDO)):
            counts[key] = (await session.execute(update(model).values(embedding=None))).rowcount
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
