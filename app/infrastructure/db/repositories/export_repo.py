from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dataset.entities import Export, Revision
from app.domain.dataset.repositories import ExportRepository
from app.infrastructure.db.mappers import apply_export, export_to_domain, revision_to_domain
from app.infrastructure.db.models import (
    DatasetExport,
    ExampleStatus,
    RevisionState,
    ReviewState,
    TrainingExample,
    TrainingExampleRevision,
)


class SqlExportRepository(ExportRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, export: Export) -> Export:
        from app.infrastructure.db.models import ExportStatus as OrmExportStatus

        row = DatasetExport(
            id=export.id,
            dataset_id=export.dataset_id,
            status=OrmExportStatus(export.status.value),
            manifest=export.manifest,
            requested_by=export.requested_by,
            object_key=export.object_key,
            checksum_sha256=export.checksum_sha256,
            item_count=export.item_count,
            error=export.error,
            created_at=export.created_at,
            completed_at=export.completed_at,
            expires_at=export.expires_at,
            expired_at=export.expired_at,
        )
        self._session.add(row)
        await self._session.flush()
        return export_to_domain(row)

    async def get(self, export_id: UUID, *, for_update: bool = False) -> Export | None:
        query = select(DatasetExport).where(DatasetExport.id == export_id)
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return export_to_domain(row) if row else None

    async def save(self, export: Export) -> None:
        row = await self._session.get(DatasetExport, export.id)
        if row is None:
            raise LookupError(f"export {export.id} does not exist")
        apply_export(row, export)
        await self._session.flush()

    async def stream_eligible_revisions(self, dataset_id: UUID) -> AsyncIterator[Revision]:
        stream = await self._session.stream_scalars(
            select(TrainingExampleRevision)
            .join(TrainingExample, TrainingExample.id == TrainingExampleRevision.example_id)
            .where(
                TrainingExample.dataset_id == dataset_id,
                TrainingExample.status == ExampleStatus.active,
                TrainingExampleRevision.id == TrainingExample.current_revision_id,
                TrainingExampleRevision.validation_status == RevisionState.valid,
                TrainingExampleRevision.review_status == ReviewState.approved,
            )
            .order_by(TrainingExampleRevision.id)
        )
        async for row in stream:
            yield revision_to_domain(row)
