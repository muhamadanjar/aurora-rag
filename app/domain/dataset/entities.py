from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID


class DatasetStatus(str, Enum):
    active = "active"
    archived = "archived"


class ExampleStatus(str, Enum):
    active = "active"
    retired = "retired"


class RevisionState(str, Enum):
    pending = "pending"
    valid = "valid"
    invalid = "invalid"


class ReviewState(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ExportStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    failed = "failed"
    expired = "expired"


DatasetFormat = Literal["chatml", "alpaca"]


@dataclass
class Dataset:
    id: UUID
    title: str
    description: str | None
    format_type: DatasetFormat
    version: int
    status: DatasetStatus
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == DatasetStatus.active


@dataclass
class Example:
    id: UUID
    dataset_id: UUID
    status: ExampleStatus
    current_revision_id: UUID | None
    version: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == ExampleStatus.active


@dataclass
class Revision:
    id: UUID
    example_id: UUID
    revision_number: int
    version: int
    content: Any
    system_prompt: str | None
    schema_version: str
    validation_status: RevisionState
    review_status: ReviewState
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    validation_errors: list[str] | None = None
    validated_by: UUID | None = None
    reviewed_by: UUID | None = None
    review_note: str | None = None
    validated_at: datetime | None = None
    reviewed_at: datetime | None = None

    @property
    def is_exportable(self) -> bool:
        return (
            self.validation_status == RevisionState.valid
            and self.review_status == ReviewState.approved
        )


@dataclass
class Export:
    id: UUID
    dataset_id: UUID
    status: ExportStatus
    manifest: dict[str, Any]
    requested_by: UUID
    created_at: datetime
    object_key: str | None = None
    checksum_sha256: str | None = None
    item_count: int | None = None
    error: str | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    expired_at: datetime | None = None


@dataclass
class NewRevision:
    content: Any
    system_prompt: str | None = None
    schema_version: str = "1"


@dataclass
class ExportManifest:
    dataset_id: UUID
    version: int
    status: str
    revision_ids: list[str] = field(default_factory=list)
