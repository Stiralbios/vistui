import pytest
from backend.database import get_async_sessionmaker
from sqlalchemy import text


@pytest.fixture(autouse=True)
async def clean_memory_tables():
    """Truncate all memory-related tables before each batch test, so every test starts
    from a clean slate regardless of the test execution order."""
    async with get_async_sessionmaker()() as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE TABLE chatgroup, chat, message, event, topic, fact, "
                "event_message_links, topic_message_links, fact_message_links "
                "RESTART IDENTITY CASCADE"
            )
        )
