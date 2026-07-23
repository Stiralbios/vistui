import os

import pytest
from backend.database import Base
from backend.main import app
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
    engine = create_async_engine(test_database_url, pool_size=1, max_overflow=0)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def client(test_database_url, test_engine):
    # Override the global engine used by the app to point at the test database.
    from backend import database as database_module
    from backend.seeders import create_default_user

    database_module._engine_instance = test_engine
    database_module._async_sessionmaker = async_sessionmaker(test_engine, expire_on_commit=False)

    await create_default_user()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    database_module._engine_instance = None
    database_module._async_sessionmaker = None
