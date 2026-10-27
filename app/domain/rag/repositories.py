from collections.abc import Iterable
from typing import Protocol

from .entities import ConfigurationEntry


class ConfigurationRepository(Protocol):
    async def get(
        self, key: str, *, for_update: bool = False
    ) -> ConfigurationEntry | None: ...

    async def list_by_keys(self, keys: Iterable[str]) -> list[ConfigurationEntry]: ...

    async def add(self, entry: ConfigurationEntry) -> ConfigurationEntry: ...

    async def save(self, entry: ConfigurationEntry) -> None: ...
