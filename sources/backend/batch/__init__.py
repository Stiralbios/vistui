"""Batch (offline) consolidation of messages into memories.

The BatchWorker (scheduled by APScheduler) runs salience scoring and the LLM
pipelines that group messages into events, topics and facts. Embeddings are computed
synchronously on the write path, so there is no separate embedding batch stage."""

from backend.batch.pipelines import EventPipeline, FactPipeline, TopicPipeline
from backend.batch.salience import SaliencePipeline, score_memory_objects
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
    "SaliencePipeline",
    "score_memory_objects",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
