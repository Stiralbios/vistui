from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, AsyncGenerator, Callable, Coroutine

from backend.settings import AppSettings
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

_engine_instance = None
_async_sessionmaker = None


class Base(DeclarativeBase):
    pass


def get_engine():
    global _engine_instance
    if _engine_instance is None:
        settings = AppSettings()
        database_url = settings.VISTUI_DATABASE_URL.get_secret_value()
        _engine_instance = create_async_engine(
            database_url,
            pool_size=10,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
        )
    return _engine_instance


def get_async_sessionmaker():
    global _async_sessionmaker
    if _async_sessionmaker is None:
        _async_sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _async_sessionmaker


async def create_db_and_tables():
    engine = get_engine()
    async with engine.begin() as conn:
        try:
            logger.info(f"Creating tables: {list(Base.metadata.tables.keys())}")
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Tables created successfully")
        except Exception as e:
            logger.error(f"Error creating tables: {e}")


@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async_sessionmaker = get_async_sessionmaker()
    async with async_sessionmaker() as session:
        async with session.begin():
            yield session


def with_async_session(func: Callable) -> Callable[..., Coroutine[Any, Any, Any]]:
    @wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        session = next((arg for arg in args if isinstance(arg, AsyncSession)), kwargs.get("session"))

        if session and isinstance(session, AsyncSession):
            return await func(*args, **kwargs)

        async with get_async_session() as session:
            return await func(session, *args, **kwargs)

    return wrapper
