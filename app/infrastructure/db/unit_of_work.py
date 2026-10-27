from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.repositories.configuration_repo import (
    SqlConfigurationRepository,
)
from app.infrastructure.db.repositories.dataset_repo import SqlDatasetRepository
from app.infrastructure.db.repositories.example_repo import SqlExampleRepository
from app.infrastructure.db.repositories.export_repo import SqlExportRepository
from app.infrastructure.db.repositories.revision_repo import SqlRevisionRepository


class SqlAlchemyUnitOfWork:
    """Transactional boundary around one SQLAlchemy session.

    Repositories only flush; the application service decides when to commit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.datasets = SqlDatasetRepository(session)
        self.examples = SqlExampleRepository(session)
        self.revisions = SqlRevisionRepository(session)
        self.exports = SqlExportRepository(session)
        self.configurations = SqlConfigurationRepository(session)

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is not None:
                await self.session.rollback()
        finally:
            await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
