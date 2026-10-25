from backend.admin.schemas import BatchStatusResponse, SeedDemoResponse, TriggerBatchResponse
from backend.batch import BatchWorker, batch_status
from backend.batch.queries import BatchQueries
from backend.chat.models import ChatDO
from backend.database import get_async_sessionmaker
from backend.message.models import MessageDO
from backend.seeders import seed_demo_chat
from fastapi import APIRouter

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
