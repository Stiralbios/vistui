"""Synchronous embedding helpers used by the write path and admin tools.

Embeddings are computed at create/update time, before the database commit, so no
separate embedding batch stage is required."""

from backend.event.models import EventDO
from backend.fact.models import FactDO
from backend.llm import Provider
from backend.topic.models import TopicDO


async def embed_text(text: str, provider: Provider | None = None) -> list[float]:
    """Compute the embedding vector for a single text."""
    provider = provider or Provider()
    vectors = await provider.embed([text])
    return vectors[0]


async def embed_texts(texts: list[str], provider: Provider | None = None) -> list[list[float]]:
    """Compute embedding vectors for a list of texts."""
    if not texts:
        return []
    provider = provider or Provider()
    return await provider.embed(texts)


async def embed_object(obj: EventDO | TopicDO | FactDO, provider: Provider | None = None) -> None:
    """Compute and assign the embedding for a single memory object in place."""
    provider = provider or Provider()
    obj.embedding = await embed_text(obj.full_text, provider)
