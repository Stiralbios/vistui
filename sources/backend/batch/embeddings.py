from backend.batch.queries import BatchQueries
from backend.database import get_async_sessionmaker
from backend.event.models import EventDO
from backend.fact.models import FactDO
from backend.llm import Provider
from backend.message.constants import ProcessingStatus
from backend.topic.models import TopicDO
from loguru import logger


async def embed_messages(limit: int = 32, provider: Provider | None = None) -> int:
    """Compute embeddings for messages with processing_state.embedding == waiting."""
    provider = provider or Provider()
    async with get_async_sessionmaker()() as session, session.begin():
        messages = await BatchQueries.get_messages_with_state(session, "embedding", limit)
        if not messages:
            return 0

        embeddings = await provider.embed([message.full_text for message in messages])
        for message, vector in zip(messages, embeddings):
            message.embedding = vector
            message.processing_state = {
                **message.processing_state,
                "embedding": ProcessingStatus.DONE,
            }

        logger.info(f"Embedded {len(messages)} messages")
        return len(messages)


async def embed_memory_objects(limit: int = 32, provider: Provider | None = None) -> int:
    """Compute embeddings for events, topics, and facts with a NULL embedding."""
    provider = provider or Provider()
    total = 0
    for model in (EventDO, TopicDO, FactDO):
        async with get_async_sessionmaker()() as session, session.begin():
            objects = await BatchQueries.get_memory_objects_without_embedding(session, model, limit)
            if not objects:
                continue

            embeddings = await provider.embed([obj.full_text for obj in objects])
            for obj, vector in zip(objects, embeddings):
                obj.embedding = vector

            logger.info(f"Embedded {len(objects)} {model.__tablename__} rows")
            total += len(objects)
    return total
