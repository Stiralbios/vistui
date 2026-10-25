import dataclasses
import uuid


@dataclasses.dataclass
class BatchStatus:
    running: bool = False
    current_pipeline: str | None = None
    current_message_id: uuid.UUID | None = None
    current_chatgroup_id: uuid.UUID | None = None
    queue_depth: int = 0
    recent_failures: int = 0


batch_status = BatchStatus()
