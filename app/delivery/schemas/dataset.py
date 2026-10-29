from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DatasetCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    format_type: Literal["chatml", "alpaca"] = "chatml"

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value


class DatasetUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value


class ExampleCreate(BaseModel):
    content: Any
    system_prompt: Optional[str] = None
    schema_version: str = Field(default="1", min_length=1, max_length=32)


class RevisionCreate(BaseModel):
    content: Any
    system_prompt: Optional[str] = None
    schema_version: str = Field(default="1", min_length=1, max_length=32)

class ReviewRequest(BaseModel):
    approved: bool
    review_note: Optional[str] = None


class ExportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    dataset_id: UUID
    status: str
    requested_by: UUID
    object_key: Optional[str] = None
    checksum_sha256: Optional[str] = None
    item_count: Optional[int] = None
    error: Optional[str] = None
    manifest: dict[str, Any]
    created_at: datetime
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None
