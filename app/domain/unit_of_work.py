from typing import Protocol

from app.domain.dataset.repositories import (
    DatasetRepository,
    ExampleRepository,
    ExportRepository,
    RevisionRepository,
)
from app.domain.rag.repositories import ConfigurationRepository


class UnitOfWork(Protocol):
    session: object
    datasets: DatasetRepository
    examples: ExampleRepository
    revisions: RevisionRepository
    exports: ExportRepository
    configurations: ConfigurationRepository

    async def __aenter__(self) -> "UnitOfWork": ...

    async def __aexit__(self, exc_type, exc, tb) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
