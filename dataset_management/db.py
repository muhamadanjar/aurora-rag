from collections.abc import AsyncGenerator
from functools import lru_cache

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .settings import get_settings


@lru_cache
def session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(create_async_engine(get_settings().database_url, pool_pre_ping=True), expire_on_commit=False, class_=AsyncSession)


def SessionLocal() -> AsyncSession:
    return session_factory()()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    try:
        session = SessionLocal()
    except ValidationError as exc:
        raise HTTPException(status_code=503, detail="Dataset management database is not configured") from exc
    async with session:
        yield session


async def get_optional_session() -> AsyncGenerator[AsyncSession | None, None]:
    try:
        session = SessionLocal()
    except ValidationError:
        yield None
        return
    async with session:
        yield session
