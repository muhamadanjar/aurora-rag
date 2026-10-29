from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.core.exceptions import (
    DatasetNotFound,
    InvalidInput,
    PreconditionFailed,
    ResourceConflict,
)
from app.domain.dataset.alpaca import validate_alpaca
from app.domain.dataset.chatml import validate_chatml
from app.domain.dataset.entities import (
    Dataset,
    DatasetFormat,
    DatasetStatus,
    Example,
    ExampleStatus,
    Revision,
    RevisionState,
    ReviewState,
)
from app.domain.unit_of_work import UnitOfWork
from app.domain.identifiers import new_uuid7


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DatasetService:
    """Dataset, example and revision use cases."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create_dataset(
        self,
        *,
        title: str,
        description: str | None,
        format_type: DatasetFormat,
        actor: UUID,
    ) -> Dataset:
        timestamp = _now()
        dataset = Dataset(
            id=new_uuid7(),
            title=title,
            description=description,
            format_type=format_type,
            version=1,
            status=DatasetStatus.active,
            created_by=actor,
            updated_by=actor,
            created_at=timestamp,
            updated_at=timestamp,
        )
        await self._uow.datasets.add(dataset)
        await self._uow.commit()
        return dataset

    async def list_datasets(self, *, page: int, per_page: int) -> tuple[list[Dataset], int]:
        return await self._uow.datasets.list(
            offset=(page - 1) * per_page, limit=per_page
        )

    async def get_dataset(self, dataset_id: UUID) -> Dataset | None:
        return await self._uow.datasets.get(dataset_id)

    async def get_active_dataset(
        self, dataset_id: UUID, *, for_update: bool = False
    ) -> Dataset:
        dataset = await self._uow.datasets.get(dataset_id, for_update=for_update)
        if dataset is None or not dataset.is_active:
            raise DatasetNotFound()
        return dataset

    async def update_dataset(
        self,
        dataset: Dataset,
        changes: dict[str, Any],
        actor: UUID,
        expected_version: int | None = None,
    ) -> Dataset:
        _require_version(dataset.version, expected_version)
        if "title" in changes and changes["title"] is None:
            raise InvalidInput("title cannot be null")
        for key, value in changes.items():
            setattr(dataset, key, value)
        dataset.version += 1
        dataset.updated_by = actor
        dataset.updated_at = _now()
        await self._uow.datasets.save(dataset)
        await self._uow.commit()
        return dataset

    async def archive_dataset(
        self,
        dataset: Dataset,
        actor: UUID,
        expected_version: int | None = None,
    ) -> Dataset:
        _require_version(dataset.version, expected_version)
        timestamp = _now()
        dataset.version += 1
        dataset.status = DatasetStatus.archived
        dataset.archived_at = timestamp
        dataset.updated_by = actor
        dataset.updated_at = timestamp
        await self._uow.datasets.save(dataset)
        await self._uow.commit()
        return dataset

    async def create_example(
        self,
        *,
        dataset_id: UUID,
        content: Any,
        system_prompt: str | None,
        schema_version: str,
        actor: UUID,
    ) -> tuple[Example, Revision]:
        await self.get_active_dataset(dataset_id, for_update=True)
        timestamp = _now()
        example = Example(
            id=new_uuid7(),
            dataset_id=dataset_id,
            status=ExampleStatus.active,
            current_revision_id=None,
            version=1,
            created_by=actor,
            updated_by=actor,
            created_at=timestamp,
            updated_at=timestamp,
        )
        revision = Revision(
            id=new_uuid7(),
            example_id=example.id,
            revision_number=1,
            version=1,
            content=content,
            system_prompt=system_prompt,
            schema_version=schema_version,
            validation_status=RevisionState.pending,
            review_status=ReviewState.pending,
            created_by=actor,
            created_at=timestamp,
            updated_at=timestamp,
        )
        await self._uow.examples.add(example)
        await self._uow.revisions.add(revision)
        example.current_revision_id = revision.id
        await self._uow.examples.save(example)
        await self._uow.commit()
        return example, revision

    async def list_examples(
        self, *, dataset_id: UUID, page: int, per_page: int
    ) -> list[tuple[Example, Revision | None]]:
        return await self._uow.examples.list_by_dataset(
            dataset_id, offset=(page - 1) * per_page, limit=per_page
        )

    async def _get_active_example(
        self, dataset_id: UUID, example_id: UUID, *, for_update: bool = False
    ) -> Example:
        example = await self._uow.examples.get(example_id, for_update=for_update)
        if example is None or example.dataset_id != dataset_id or not example.is_active:
            raise DatasetNotFound()
        return example

    async def _get_revision(
        self,
        example_id: UUID,
        revision_id: UUID,
        *,
        for_update: bool = False,
    ) -> Revision:
        revision = await self._uow.revisions.get(revision_id, for_update=for_update)
        if revision is None or revision.example_id != example_id:
            raise DatasetNotFound()
        return revision

    async def create_revision(
        self,
        *,
        dataset_id: UUID,
        example_id: UUID,
        content: Any,
        system_prompt: str | None,
        schema_version: str,
        actor: UUID,
        expected_example_version: int | None,
    ) -> tuple[Example, Revision]:
        example = await self._get_active_example(
            dataset_id, example_id, for_update=True
        )
        _require_version(example.version, expected_example_version)

        latest = await self._uow.revisions.latest_number(example_id)
        timestamp = _now()
        revision = Revision(
            id=new_uuid7(),
            example_id=example_id,
            revision_number=latest + 1,
            version=1,
            content=content,
            system_prompt=system_prompt,
            schema_version=schema_version,
            validation_status=RevisionState.pending,
            review_status=ReviewState.pending,
            created_by=actor,
            created_at=timestamp,
            updated_at=timestamp,
        )
        example.version += 1
        example.updated_at = timestamp
        example.current_revision_id = revision.id

        await self._uow.revisions.add(revision)
        await self._uow.examples.save(example)
        await self._uow.commit()
        return example, revision

    async def validate_revision(
        self,
        *,
        dataset_id: UUID,
        example_id: UUID,
        revision_id: UUID,
        actor: UUID,
        expected_revision_version: int | None,
    ) -> tuple[Revision, list[str]]:
        dataset = await self.get_active_dataset(dataset_id, for_update=True)
        await self._get_active_example(dataset_id, example_id, for_update=True)
        revision = await self._get_revision(
            example_id, revision_id, for_update=True
        )
        _require_version(revision.version, expected_revision_version)

        errors = (
            validate_chatml(revision.content)
            if dataset.format_type == "chatml"
            else validate_alpaca(revision.content)
        )
        timestamp = _now()
        revision.validation_errors = errors or None
        revision.validation_status = (
            RevisionState.valid if not errors else RevisionState.invalid
        )
        revision.validated_by = actor
        revision.validated_at = timestamp
        revision.updated_at = timestamp
        revision.version += 1

        await self._uow.revisions.save(revision)
        await self._uow.commit()
        return revision, errors

    async def review_revision(
        self,
        *,
        dataset_id: UUID,
        example_id: UUID,
        revision_id: UUID,
        approved: bool,
        review_note: str | None,
        actor: UUID,
        expected_revision_version: int | None,
    ) -> tuple[Revision, Example]:
        example = await self._get_active_example(
            dataset_id, example_id, for_update=True
        )
        revision = await self._get_revision(
            example_id, revision_id, for_update=True
        )
        _require_version(revision.version, expected_revision_version)
        if revision.validation_status != RevisionState.valid:
            raise ResourceConflict("Revision must validate before review")

        timestamp = _now()
        revision.review_status = (
            ReviewState.approved if approved else ReviewState.rejected
        )
        revision.reviewed_by = actor
        revision.reviewed_at = timestamp
        revision.review_note = review_note
        revision.updated_at = timestamp
        revision.version += 1

        if approved and example.current_revision_id != revision.id:
            example.current_revision_id = revision.id
            example.version += 1
            example.updated_by = actor
            example.updated_at = timestamp

        await self._uow.revisions.save(revision)
        await self._uow.examples.save(example)
        await self._uow.commit()
        return revision, example

    async def retire_example(
        self,
        *,
        dataset_id: UUID,
        example_id: UUID,
        actor: UUID,
        expected_example_version: int | None,
    ) -> Example:
        example = await self._get_active_example(
            dataset_id, example_id, for_update=True
        )
        _require_version(example.version, expected_example_version)

        timestamp = _now()
        example.status = ExampleStatus.retired
        example.retired_at = timestamp
        example.updated_by = actor
        example.updated_at = timestamp
        example.version += 1

        await self._uow.examples.save(example)
        await self._uow.commit()
        return example


def _require_version(current: int, expected: int | None) -> None:
    if expected is not None and current != expected:
        raise PreconditionFailed()
