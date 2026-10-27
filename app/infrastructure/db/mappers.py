from app.domain.dataset.entities import (
    Dataset as DatasetEntity,
    DatasetStatus,
    Example as ExampleEntity,
    ExampleStatus,
    Export as ExportEntity,
    ExportStatus,
    Revision as RevisionEntity,
    RevisionState,
    ReviewState,
)
from app.domain.rag.entities import ConfigurationEntry

from .models import (
    Configuration,
    DatasetExport,
    TrainingDataset,
    TrainingExample,
    TrainingExampleRevision,
)


def dataset_to_domain(row: TrainingDataset) -> DatasetEntity:
    return DatasetEntity(
        id=row.id,
        title=row.title,
        description=row.description,
        format_type=row.format_type,
        version=row.version,
        status=DatasetStatus(row.status.value),
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


def apply_dataset(row: TrainingDataset, entity: DatasetEntity) -> None:
    from .models import DatasetStatus as OrmDatasetStatus

    row.title = entity.title
    row.description = entity.description
    row.format_type = entity.format_type
    row.version = entity.version
    row.status = OrmDatasetStatus(entity.status.value)
    row.updated_by = entity.updated_by
    row.updated_at = entity.updated_at
    row.archived_at = entity.archived_at


def example_to_domain(row: TrainingExample) -> ExampleEntity:
    return ExampleEntity(
        id=row.id,
        dataset_id=row.dataset_id,
        status=ExampleStatus(row.status.value),
        current_revision_id=row.current_revision_id,
        version=row.version,
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        retired_at=row.retired_at,
    )


def apply_example(row: TrainingExample, entity: ExampleEntity) -> None:
    from .models import ExampleStatus as OrmExampleStatus

    row.status = OrmExampleStatus(entity.status.value)
    row.current_revision_id = entity.current_revision_id
    row.version = entity.version
    row.updated_by = entity.updated_by
    row.updated_at = entity.updated_at
    row.retired_at = entity.retired_at


def revision_to_domain(row: TrainingExampleRevision) -> RevisionEntity:
    return RevisionEntity(
        id=row.id,
        example_id=row.example_id,
        revision_number=row.revision_number,
        version=row.version,
        content=row.content,
        system_prompt=row.system_prompt,
        schema_version=row.schema_version,
        validation_status=RevisionState(row.validation_status.value),
        review_status=ReviewState(row.review_status.value),
        validation_errors=row.validation_errors,
        created_by=row.created_by,
        validated_by=row.validated_by,
        reviewed_by=row.reviewed_by,
        review_note=row.review_note,
        created_at=row.created_at,
        validated_at=row.validated_at,
        reviewed_at=row.reviewed_at,
        updated_at=row.updated_at,
    )


def apply_revision(row: TrainingExampleRevision, entity: RevisionEntity) -> None:
    from .models import RevisionState as OrmRevisionState, ReviewState as OrmReviewState

    row.version = entity.version
    row.content = entity.content
    row.system_prompt = entity.system_prompt
    row.schema_version = entity.schema_version
    row.validation_status = OrmRevisionState(entity.validation_status.value)
    row.review_status = OrmReviewState(entity.review_status.value)
    row.validation_errors = entity.validation_errors
    row.validated_by = entity.validated_by
    row.reviewed_by = entity.reviewed_by
    row.review_note = entity.review_note
    row.validated_at = entity.validated_at
    row.reviewed_at = entity.reviewed_at
    row.updated_at = entity.updated_at


def export_to_domain(row: DatasetExport) -> ExportEntity:
    return ExportEntity(
        id=row.id,
        dataset_id=row.dataset_id,
        status=ExportStatus(row.status.value),
        manifest=row.manifest,
        requested_by=row.requested_by,
        object_key=row.object_key,
        checksum_sha256=row.checksum_sha256,
        item_count=row.item_count,
        error=row.error,
        created_at=row.created_at,
        completed_at=row.completed_at,
        expires_at=row.expires_at,
        expired_at=row.expired_at,
    )


def apply_export(row: DatasetExport, entity: ExportEntity) -> None:
    from .models import ExportStatus as OrmExportStatus

    row.status = OrmExportStatus(entity.status.value)
    row.manifest = entity.manifest
    row.object_key = entity.object_key
    row.checksum_sha256 = entity.checksum_sha256
    row.item_count = entity.item_count
    row.error = entity.error
    row.completed_at = entity.completed_at
    row.expires_at = entity.expires_at
    row.expired_at = entity.expired_at


def configuration_to_domain(row: Configuration) -> ConfigurationEntry:
    return ConfigurationEntry(id=row.id, key=row.key, value=row.value)


def apply_configuration(row: Configuration, entity: ConfigurationEntry) -> None:
    row.value = entity.value
