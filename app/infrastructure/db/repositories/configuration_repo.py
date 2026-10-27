from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.rag.entities import ConfigurationEntry
from app.domain.rag.repositories import ConfigurationRepository
from app.infrastructure.db.mappers import apply_configuration, configuration_to_domain
from app.infrastructure.db.models import Configuration


class SqlConfigurationRepository(ConfigurationRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, key: str, *, for_update: bool = False
    ) -> ConfigurationEntry | None:
        query = select(Configuration).where(Configuration.key == key)
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return configuration_to_domain(row) if row else None

    async def list_by_keys(self, keys: Iterable[str]) -> list[ConfigurationEntry]:
        result = await self._session.execute(
            select(Configuration).where(Configuration.key.in_(tuple(keys)))
        )
        return [configuration_to_domain(row) for row in result.scalars()]

    async def add(self, entry: ConfigurationEntry) -> ConfigurationEntry:
        row = Configuration(id=entry.id, key=entry.key, value=entry.value)
        self._session.add(row)
        await self._session.flush()
        return configuration_to_domain(row)

    async def save(self, entry: ConfigurationEntry) -> None:
        row = await self._session.get(Configuration, entry.id)
        if row is None:
            raise LookupError(f"configuration {entry.key} does not exist")
        apply_configuration(row, entry)
        await self._session.flush()
