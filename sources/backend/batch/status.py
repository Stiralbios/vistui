"""In-memory snapshot of the batch worker's progress, exposed by the admin API.
Not persisted: values reset on process restart.
"""

import dataclasses
import uuid


@dataclasses.dataclass
class BatchStatus:
    """Current state of the batch: what is running, on which message, and how much
    work is left (queue_depth) or has failed recently."""

    running: bool = False
    current_pipeline: str | None = None
    current_message_id: uuid.UUID | None = None
    current_chatgroup_id: uuid.UUID | None = None
    queue_depth: int = 0
    recent_failures: int = 0


# Process-wide singleton: mutated by BatchWorker, read by the admin API.
batch_status = BatchStatus()
