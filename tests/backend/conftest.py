import os
from unittest.mock import patch

# Mark tests as running in the TEST environment before any app modules are imported.
os.environ.setdefault("APP_ENVIRONMENT", "TEST")

import pytest
from backend.database import Base
from backend.main import app
from backend.seeders import create_default_user
from httpx import ASGITransport, AsyncClient
from langchain_openai import OpenAIEmbeddings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _fake_embeddings_model():
    """Return a LangChain OpenAIEmbeddings whose aembed_documents returns deterministic
    vectors. Patched globally for backend tests so message/memory creation can compute
    embeddings synchronously without calling a real provider."""

    class FakeEmbeddings(OpenAIEmbeddings):
        def __init__(self, **kwargs):
            pass

        async def aembed_documents(
            self, texts: list[str], chunk_size: int | None = None, **kwargs
        ) -> list[list[float]]:
            return [[0.01 * i] + [0.0] * 767 for i in range(len(texts))]

        async def aembed_query(self, text: str, **kwargs) -> list[float]:
            return [0.0] * 768

    return FakeEmbeddings


@pytest.fixture(autouse=True)
def patch_embeddings_globally():
    with patch("backend.llm.provider.OpenAIEmbeddings", _fake_embeddings_model()):
        yield


@pytest.fixture(scope="session")
def test_database_url():
    return os.environ.get(
        "VISTUI_TEST_DATABASE_URL",
        "postgresql+psycopg_async://vistui:vistui@localhost:5434/vistui",
    )


@pytest.fixture(scope="session")
async def test_engine(test_database_url):
    from backend import database as database_module

    engine = create_async_engine(test_database_url, pool_size=1, max_overflow=0)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    database_module._engine_instance = engine
    database_module._async_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    yield engine

    # The data is intentionally kept after the session for inspection (browse the
    # vistui_postgres_test container in Adminer); the next test run starts by dropping
    # and recreating every table anyway.
    database_module._engine_instance = None
    database_module._async_sessionmaker = None
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
async def seed_default_user(test_engine):
    await create_default_user()


@pytest.fixture
async def client(test_engine):
    # Override the global engine used by the app to point at the test database.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
