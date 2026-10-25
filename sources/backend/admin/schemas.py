import uuid

from pydantic import BaseModel, ConfigDict


class SeedDemoResponse(BaseModel):
    chatgroup_id: uuid.UUID | None
    chat_id: uuid.UUID | None
    message_count: int


class TriggerBatchResponse(BaseModel):
    status: str


class BatchStatusResponse(BaseModel):
    running: bool
    current_pipeline: str | None
    current_message_id: uuid.UUID | None
    current_chatgroup_id: uuid.UUID | None
    queue_depth: int
    recent_failures: int

    model_config = ConfigDict(from_attributes=True)
