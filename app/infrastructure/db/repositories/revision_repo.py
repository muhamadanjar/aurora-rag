from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dataset.entities import Revision
from app.domain.dataset.repositories import RevisionRepository
from app.infrastructure.db.mappers import apply_revision, revision_to_domain
from app.infrastructure.db.models import (
    RevisionState as OrmRevisionState,
    ReviewState as OrmReviewState,
    TrainingExampleRevision,
)


class SqlRevisionRepository(RevisionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, revision: Revision) -> Revision:
        row = TrainingExampleRevision(
            id=revision.id,
            example_id=revision.example_id,
            revision_number=revision.revision_number,
            version=revision.version,
            content=revision.content,
            system_prompt=revision.system_prompt,
            schema_version=revision.schema_version,
            validation_status=OrmRevisionState(revision.validation_status.value),
            review_status=OrmReviewState(revision.review_status.value),
            validation_errors=revision.validation_errors,
            created_by=revision.created_by,
            validated_by=revision.validated_by,
            reviewed_by=revision.reviewed_by,
            review_note=revision.review_note,
            created_at=revision.created_at,
            validated_at=revision.validated_at,
            reviewed_at=revision.reviewed_at,
            updated_at=revision.updated_at,
        )
        self._session.add(row)
        await self._session.flush()
        return revision_to_domain(row)

    async def get(self, revision_id: UUID, *, for_update: bool = False) -> Revision | None:
        query = select(TrainingExampleRevision).where(
            TrainingExampleRevision.id == revision_id
        )
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return revision_to_domain(row) if row else None

    async def latest_number(self, example_id: UUID) -> int:
        latest = await self._session.scalar(
            select(func.max(TrainingExampleRevision.revision_number)).where(
                TrainingExampleRevision.example_id == example_id
            )
        )
        return int(latest or 0)

    async def save(self, revision: Revision) -> None:
        row = await self._session.get(TrainingExampleRevision, revision.id)
        if row is None:
            raise LookupError(f"revision {revision.id} does not exist")
        apply_revision(row, revision)
        await self._session.flush()
