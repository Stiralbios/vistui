from enum import StrEnum


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ProcessingStatus(StrEnum):
    WAITING = "waiting"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
