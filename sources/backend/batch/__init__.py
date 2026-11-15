"""Batch (offline) consolidation of messages into memories.

Messages are processed in rounds by the BatchWorker (scheduled by APScheduler):
embedding first, then LLM pipelines that group messages into events, topics and facts.
"""

from backend.batch.embeddings import embed_memory_objects, embed_messages
from backend.batch.pipelines import EventPipeline, FactPipeline, TopicPipeline
from backend.batch.scheduler import get_scheduler, start_scheduler, stop_scheduler
from backend.batch.status import BatchStatus, batch_status
from backend.batch.worker import BatchWorker

__all__ = [
    "BatchWorker",
    "BatchStatus",
    "EventPipeline",
    "FactPipeline",
    "TopicPipeline",
    "batch_status",
    "embed_messages",
    "embed_memory_objects",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
