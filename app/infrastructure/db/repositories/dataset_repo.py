from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dataset.entities import Dataset
from app.domain.dataset.repositories import DatasetRepository
from app.infrastructure.db.mappers import apply_dataset, dataset_to_domain
from app.infrastructure.db.models import DatasetStatus as OrmDatasetStatus
from app.infrastructure.db.models import TrainingDataset


class SqlDatasetRepository(DatasetRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, dataset: Dataset) -> Dataset:
        row = TrainingDataset(
            id=dataset.id,
            title=dataset.title,
            description=dataset.description,
            format_type=dataset.format_type,
            version=dataset.version,
            status=OrmDatasetStatus(dataset.status.value),
            created_by=dataset.created_by,
            updated_by=dataset.updated_by,
            created_at=dataset.created_at,
            updated_at=dataset.updated_at,
            archived_at=dataset.archived_at,
        )
        self._session.add(row)
        await self._session.flush()
        return dataset_to_domain(row)

    async def get(self, dataset_id: UUID, *, for_update: bool = False) -> Dataset | None:
        query = select(TrainingDataset).where(TrainingDataset.id == dataset_id)
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return dataset_to_domain(row) if row else None

    async def list(self, *, offset: int, limit: int) -> tuple[list[Dataset], int]:
        total = await self._session.scalar(select(func.count()).select_from(TrainingDataset))
        result = await self._session.execute(
            select(TrainingDataset)
            .order_by(TrainingDataset.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [dataset_to_domain(row) for row in result.scalars()], int(total or 0)

    async def save(self, dataset: Dataset) -> None:
        row = await self._session.get(TrainingDataset, dataset.id)
        if row is None:
            raise LookupError(f"dataset {dataset.id} does not exist")
        apply_dataset(row, dataset)
        await self._session.flush()
