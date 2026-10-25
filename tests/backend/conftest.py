import os

# Mark tests as running in the TEST environment before any app modules are imported.
os.environ.setdefault("APP_ENVIRONMENT", "TEST")

import pytest
from backend.database import Base
from backend.main import app
from backend.seeders import create_default_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


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

    database_module._engine_instance = None
    database_module._async_sessionmaker = None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
async def seed_default_user(test_engine):
    await create_default_user()


@pytest.fixture
async def client(test_engine):
    # Override the global engine used by the app to point at the test database.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
