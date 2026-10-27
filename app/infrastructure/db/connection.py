from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


@lru_cache
def engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False, class_=AsyncSession)


def SessionLocal() -> AsyncSession:
    return session_factory()()


def create_unit_of_work():
    from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

    return SqlAlchemyUnitOfWork(SessionLocal())

