import asyncio
from collections.abc import Callable
from app.core.exceptions import ConfigurationInvalid, ResourceConflict
from app.domain.rag.entities import ConfigurationEntry
from app.domain.unit_of_work import UnitOfWork
from app.domain.identifiers import new_uuid7
from .config_service import (
    DEFAULT_CONFIGURATION,
    list_configuration,
    validate_configuration_value,
)


class RuntimeConfigService:
    def __init__(
        self,
        uow: UnitOfWork,
        *,
        vector_count: Callable[[], int],
        model_lock_factory: Callable[[], object] | None = None,
    ) -> None:
        self._uow = uow
        self._vector_count = vector_count
        self._model_lock_factory = model_lock_factory

    async def list(self) -> list[ConfigurationEntry]:
        return await list_configuration(self._uow.configurations)

    async def update(self, key: str, value: str) -> ConfigurationEntry:
        model_lock = None
        if key == "embedding.model" and self._model_lock_factory is not None:
            model_lock = await asyncio.to_thread(self._model_lock_factory)
        try:
            return await self._update_locked(key, value)
        finally:
            if model_lock is not None:
                model_lock.release()

    async def _update_locked(self, key: str, value: str) -> ConfigurationEntry:
        try:
            normalized = validate_configuration_value(key, value)
        except ValueError as exc:
            raise ConfigurationInvalid(str(exc)) from exc

        current = await self._uow.configurations.get(key, for_update=True)
        current_value = (
            current.value if current is not None else DEFAULT_CONFIGURATION[key]
        )
        if key == "embedding.model" and current_value != normalized:
            if await asyncio.to_thread(self._vector_count) > 0:
                raise ResourceConflict(
                    "embedding.model cannot change after indexed data exists"
                )

        if current is None:
            current = ConfigurationEntry(id=new_uuid7(), key=key, value=normalized)
            await self._uow.configurations.add(current)
        else:
            current.value = normalized
            await self._uow.configurations.save(current)
        await self._uow.commit()
        return current

