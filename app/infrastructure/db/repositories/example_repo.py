from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dataset.entities import Example, Revision
from app.domain.dataset.repositories import ExampleRepository
from app.infrastructure.db.mappers import apply_example, example_to_domain, revision_to_domain
from app.infrastructure.db.models import (
    ExampleStatus as OrmExampleStatus,
    TrainingExample,
    TrainingExampleRevision,
)


class SqlExampleRepository(ExampleRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, example: Example) -> Example:
        row = TrainingExample(
            id=example.id,
            dataset_id=example.dataset_id,
            status=OrmExampleStatus(example.status.value),
            current_revision_id=example.current_revision_id,
            version=example.version,
            created_by=example.created_by,
            updated_by=example.updated_by,
            created_at=example.created_at,
            updated_at=example.updated_at,
            retired_at=example.retired_at,
        )
        self._session.add(row)
        await self._session.flush()
        return example_to_domain(row)

    async def get(self, example_id: UUID, *, for_update: bool = False) -> Example | None:
        query = select(TrainingExample).where(TrainingExample.id == example_id)
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return example_to_domain(row) if row else None

    async def list_by_dataset(
        self, dataset_id: UUID, *, offset: int, limit: int
    ) -> list[tuple[Example, Revision | None]]:
        result = await self._session.execute(
            select(TrainingExample, TrainingExampleRevision)
            .outerjoin(
                TrainingExampleRevision,
                TrainingExampleRevision.id == TrainingExample.current_revision_id,
            )
            .where(TrainingExample.dataset_id == dataset_id)
            .order_by(TrainingExample.created_at, TrainingExample.id)
            .offset(offset)
            .limit(limit)
        )
        return [
            (example_to_domain(example), revision_to_domain(revision) if revision else None)
            for example, revision in result.all()
        ]

    async def save(self, example: Example) -> None:
        row = await self._session.get(TrainingExample, example.id)
        if row is None:
            raise LookupError(f"example {example.id} does not exist")
        apply_example(row, example)
        await self._session.flush()
