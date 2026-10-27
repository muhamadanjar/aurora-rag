from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.domain.identifiers import new_uuid7


def now() -> datetime:
    return datetime.now(timezone.utc)


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


class TrainingDataset(SQLModel, table=True):
    __tablename__ = "training_datasets"
    id: UUID = Field(default_factory=new_uuid7, primary_key=True)
    title: str = Field(sa_column=Column(String(200), nullable=False))
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    format_type: Literal["chatml"] = Field(default="chatml", sa_column=Column(String(20), nullable=False))
    version: int = Field(default=1, nullable=False)
    status: DatasetStatus = Field(default=DatasetStatus.active, sa_column=Column(SAEnum(DatasetStatus, name="dataset_status", native_enum=True), nullable=False))
    created_by: UUID
    updated_by: UUID
    created_at: datetime = Field(default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False))
    archived_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    __table_args__ = (CheckConstraint("format_type = 'chatml'", name="ck_training_datasets_format_type_chatml"),)

class TrainingExample(SQLModel, table=True):
    __tablename__ = "training_examples"
    id: UUID = Field(default_factory=new_uuid7, primary_key=True)
    dataset_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), ForeignKey("training_datasets.id", name="fk_training_examples_dataset_id"), nullable=False))
    status: ExampleStatus = Field(default=ExampleStatus.active, sa_column=Column(SAEnum(ExampleStatus, name="example_status", native_enum=True), nullable=False))
    current_revision_id: Optional[UUID] = Field(default=None, sa_column=Column(PGUUID(as_uuid=True), ForeignKey("training_example_revisions.id", name="fk_training_examples_current_revision_id", deferrable=True, initially="DEFERRED", use_alter=True)))
    version: int = Field(default=1, nullable=False)
    created_by: UUID
    updated_by: UUID
    created_at: datetime = Field(default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False))
    retired_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    __table_args__ = (Index("ix_training_examples_dataset_created", "dataset_id", "created_at", "id"),)


class TrainingExampleRevision(SQLModel, table=True):
    __tablename__ = "training_example_revisions"
    id: UUID = Field(default_factory=new_uuid7, primary_key=True)
    example_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), ForeignKey("training_examples.id", name="fk_training_example_revisions_example_id"), nullable=False))
    revision_number: int = Field(sa_column=Column(Integer, nullable=False))
    version: int = Field(default=1, nullable=False)
    content: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    system_prompt: Optional[str] = Field(default=None, sa_column=Column(Text))
    schema_version: str = Field(default="1", sa_column=Column(String(32), nullable=False))
    validation_status: RevisionState = Field(default=RevisionState.pending, sa_column=Column(SAEnum(RevisionState, name="revision_validation_status", native_enum=True), nullable=False))
    review_status: ReviewState = Field(default=ReviewState.pending, sa_column=Column(SAEnum(ReviewState, name="revision_review_status", native_enum=True), nullable=False))
    validation_errors: Optional[list[str]] = Field(default=None, sa_column=Column(JSONB))
    created_by: UUID
    validated_by: Optional[UUID] = None
    reviewed_by: Optional[UUID] = None
    review_note: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False))
    validated_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    reviewed_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False))

    __table_args__ = (
        UniqueConstraint("example_id", "revision_number", name="uq_training_example_revisions_example_revision_number"),
        Index("ix_training_example_revisions_example_created", "example_id", "created_at"),
    )


class DatasetExport(SQLModel, table=True):
    __tablename__ = "dataset_exports"
    id: UUID = Field(default_factory=new_uuid7, primary_key=True)
    dataset_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), ForeignKey("training_datasets.id", name="fk_dataset_exports_dataset_id"), nullable=False))
    status: ExportStatus = Field(default=ExportStatus.pending, sa_column=Column(SAEnum(ExportStatus, name="export_status", native_enum=True), nullable=False))
    manifest: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    requested_by: UUID
    object_key: Optional[str] = Field(default=None, sa_column=Column(Text))
    checksum_sha256: Optional[str] = Field(default=None, sa_column=Column(String(64)))
    item_count: Optional[int] = Field(default=None, sa_column=Column(Integer))
    error: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False))
    completed_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    expires_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    expired_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    __table_args__ = (Index("ix_dataset_exports_dataset_created", "dataset_id", "created_at"),)
class Configuration(SQLModel, table=True):
    __tablename__ = "configuration"

    id: UUID = Field(default_factory=new_uuid7, primary_key=True)
    key: str = Field(sa_column=Column("key", Text, nullable=False))
    value: str = Field(sa_column=Column("value", Text, nullable=False))

    __table_args__ = (UniqueConstraint("key", name="uq_configuration_key"),)
