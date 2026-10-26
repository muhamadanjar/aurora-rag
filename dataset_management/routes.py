import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import clear_failed_logins, current_operator, create_token, require_settings, reserve_login_attempt, verify_password
from .db import get_session
from .models import DatasetExport, DatasetStatus, ExampleStatus, ExportStatus, RevisionState, ReviewState, TrainingDataset, TrainingExample, TrainingExampleRevision, now
from .schemas import DatasetCreate, DatasetUpdate, ExampleCreate, ExportResponse, ReviewRequest, RevisionCreate, TokenResponse
from .service import create_export, validate_revision
from .service import _remove_export_artifact, _safe_export_path
from .settings import Settings

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/auth/token", response_model=TokenResponse, tags=["auth"])
async def token(request: Request, form: OAuth2PasswordRequestForm = Depends(), settings: Settings = Depends(require_settings)):
    login_key = request.client.host if request.client else "unknown"
    if not reserve_login_attempt(login_key):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts", headers={"Retry-After": "60"})
    if form.username != settings.auth_username or not await verify_password(form.password, settings.auth_password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    clear_failed_logins(login_key)
    return TokenResponse(access_token=create_token(settings.operator_uuid, settings))


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Resource not found")


def etag(value: Any) -> str:
    if getattr(value, "__tablename__", None) in {"training_datasets", "training_examples", "training_example_revisions"}:
        return f'"{value.version}"'
    timestamp = getattr(value, "updated_at", getattr(value, "created_at", now()))
    return f'"{timestamp.timestamp():.6f}"'


def require_if_match(value: Any, provided: str | None) -> None:
    if provided is None:
        raise HTTPException(status_code=428, detail="If-Match header is required")
    if provided != "*" and provided != etag(value):
        raise HTTPException(status_code=412, detail="Resource has changed")


async def active_dataset(session: AsyncSession, dataset_id: UUID, *, for_update: bool = False) -> TrainingDataset:
    query = select(TrainingDataset).where(TrainingDataset.id == dataset_id)
    if for_update:
        query = query.with_for_update()
    dataset = await session.scalar(query)
    if not dataset or dataset.status != DatasetStatus.active:
        raise not_found()
    return dataset


@router.post("/datasets", status_code=201)
async def create_dataset(body: DatasetCreate, response: Response, session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    dataset = TrainingDataset(**body.model_dump(), created_by=actor, updated_by=actor)
    session.add(dataset)
    await session.commit()
    await session.refresh(dataset)
    response.headers["ETag"] = etag(dataset)
    return dataset


@router.get("/datasets")
async def list_datasets(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100), session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    total = await session.scalar(select(func.count()).select_from(TrainingDataset))
    result = await session.execute(select(TrainingDataset).order_by(TrainingDataset.created_at.desc()).offset((page - 1) * per_page).limit(per_page))
    return {"data": result.scalars().all(), "meta": {"pagination": {"page": page, "per_page": per_page, "total": total or 0, "total_pages": ((total or 0) + per_page - 1) // per_page}}}


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: UUID, response: Response, session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    dataset = await session.get(TrainingDataset, dataset_id)
    if not dataset: raise not_found()
    response.headers["ETag"] = etag(dataset)
    return dataset


@router.patch("/datasets/{dataset_id}")
async def update_dataset(dataset_id: UUID, body: DatasetUpdate, response: Response, if_match: str | None = Header(default=None), session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    dataset = await active_dataset(session, dataset_id, for_update=True)
    require_if_match(dataset, if_match)
    if "title" in body.model_fields_set and body.title is None:
        raise HTTPException(status_code=422, detail="title cannot be null")
    for key, value in body.model_dump(exclude_unset=True).items(): setattr(dataset, key, value)
    dataset.version += 1
    dataset.updated_by, dataset.updated_at = actor, now()
    await session.commit()
    await session.refresh(dataset)
    response.headers["ETag"] = etag(dataset)
    return dataset


@router.post("/datasets/{dataset_id}/archive", status_code=204)
async def archive_dataset(dataset_id: UUID, if_match: str | None = Header(default=None), session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    dataset = await active_dataset(session, dataset_id, for_update=True)
    require_if_match(dataset, if_match)
    dataset.version += 1
    dataset.status, dataset.archived_at, dataset.updated_by, dataset.updated_at = DatasetStatus.archived, now(), actor, now()
    await session.commit()


@router.post("/datasets/{dataset_id}/examples", status_code=201)
async def create_example(dataset_id: UUID, body: ExampleCreate, response: Response, session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    await active_dataset(session, dataset_id, for_update=True)
    example = TrainingExample(dataset_id=dataset_id, created_by=actor, updated_by=actor)
    revision = TrainingExampleRevision(example_id=example.id, revision_number=1, version=1, content=body.content, system_prompt=body.system_prompt, schema_version=body.schema_version, created_by=actor)
    example.current_revision_id = revision.id
    session.add_all([example, revision])
    await session.commit()
    await session.refresh(example)
    await session.refresh(revision)
    response.headers["ETag"] = etag(example)
    response.headers["Revision-ETag"] = etag(revision)
    return {"example": example, "revision": revision}


@router.get("/datasets/{dataset_id}/examples")
async def list_examples(dataset_id: UUID, page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100), session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    await active_dataset(session, dataset_id)
    result = await session.execute(select(TrainingExample).where(TrainingExample.dataset_id == dataset_id).order_by(TrainingExample.created_at).offset((page - 1) * per_page).limit(per_page))
    return result.scalars().all()


@router.post("/datasets/{dataset_id}/examples/{example_id}/revisions", status_code=201)
async def create_revision(dataset_id: UUID, example_id: UUID, body: RevisionCreate, response: Response, if_match: str | None = Header(default=None), session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    await active_dataset(session, dataset_id, for_update=True)
    example = await session.scalar(select(TrainingExample).where(TrainingExample.id == example_id).with_for_update())
    if not example or example.dataset_id != dataset_id or example.status != ExampleStatus.active: raise not_found()
    require_if_match(example, if_match)
    latest = await session.scalar(select(func.max(TrainingExampleRevision.revision_number)).where(TrainingExampleRevision.example_id == example_id)) or 0
    revision = TrainingExampleRevision(example_id=example_id, revision_number=latest + 1, version=1, content=body.content, system_prompt=body.system_prompt, schema_version=body.schema_version, created_by=actor)
    example.version += 1
    example.updated_at = now()
    example.current_revision_id = revision.id
    session.add(revision)
    await session.commit()
    await session.refresh(revision)
    await session.refresh(example)
    response.headers["ETag"] = etag(example)
    response.headers["Revision-ETag"] = etag(revision)
    return revision


@router.post("/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/validate")
async def validate(dataset_id: UUID, example_id: UUID, revision_id: UUID, response: Response, if_match: str | None = Header(default=None), session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    await active_dataset(session, dataset_id, for_update=True)
    example = await session.get(TrainingExample, example_id)
    if not example or example.dataset_id != dataset_id or example.status != ExampleStatus.active: raise not_found()
    revision = await session.scalar(select(TrainingExampleRevision).where(TrainingExampleRevision.id == revision_id).with_for_update())
    if not revision or revision.example_id != example_id: raise not_found()
    require_if_match(revision, if_match)
    errors = await validate_revision(session, revision, actor)
    await session.commit()
    if errors:
        raise HTTPException(status_code=422, detail={"code": "INVALID_CHATML", "errors": errors})
    response.headers["ETag"] = etag(revision)
    return revision


@router.post("/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/review")
async def review(dataset_id: UUID, example_id: UUID, revision_id: UUID, body: ReviewRequest, response: Response, if_match: str | None = Header(default=None), session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    await active_dataset(session, dataset_id, for_update=True)
    example = await session.scalar(select(TrainingExample).where(TrainingExample.id == example_id).with_for_update())
    if not example or example.dataset_id != dataset_id or example.status != ExampleStatus.active: raise not_found()
    revision = await session.scalar(select(TrainingExampleRevision).where(TrainingExampleRevision.id == revision_id).with_for_update())
    if not revision or revision.example_id != example_id: raise not_found()
    require_if_match(revision, if_match)
    if revision.validation_status != RevisionState.valid: raise HTTPException(status_code=409, detail="Revision must validate before review")
    revision.review_status = ReviewState.approved if body.approved else ReviewState.rejected
    revision.reviewed_by, revision.reviewed_at, revision.review_note = actor, now(), body.review_note
    if body.approved and example.current_revision_id != revision.id:
        example.current_revision_id = revision.id
        example.version += 1
        example.updated_by = actor
        example.updated_at = now()
    revision.updated_at = now()
    revision.version += 1
    await session.commit()
    response.headers["ETag"] = etag(revision)
    response.headers["Example-ETag"] = etag(example)
    return revision


@router.post("/datasets/{dataset_id}/examples/{example_id}/retire", status_code=204)
async def retire_example(dataset_id: UUID, example_id: UUID, response: Response, if_match: str | None = Header(default=None), session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    await active_dataset(session, dataset_id, for_update=True)
    example = await session.get(TrainingExample, example_id)
    if not example or example.dataset_id != dataset_id or example.status != ExampleStatus.active: raise not_found()
    require_if_match(example, if_match)
    example.status, example.retired_at, example.updated_by, example.updated_at = ExampleStatus.retired, now(), actor, now()
    example.version += 1
    await session.commit()
    response.headers["ETag"] = etag(example)


@router.post("/datasets/{dataset_id}/exports", response_model=ExportResponse, status_code=201)
async def export_dataset(dataset_id: UUID, session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator), settings: Settings = Depends(require_settings)):
    dataset = await active_dataset(session, dataset_id, for_update=True)
    try:
        return await create_export(session, dataset, actor, settings)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/datasets/{dataset_id}/exports/{export_id}", response_model=ExportResponse)
async def export_status(dataset_id: UUID, export_id: UUID, session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator)):
    export = await session.get(DatasetExport, export_id)
    if not export or export.dataset_id != dataset_id: raise not_found()
    return export


@router.get("/datasets/{dataset_id}/exports/{export_id}/download")
async def download_export(dataset_id: UUID, export_id: UUID, session: AsyncSession = Depends(get_session), actor: UUID = Depends(current_operator), settings: Settings = Depends(require_settings)):
    export = await session.get(DatasetExport, export_id)
    if not export or export.dataset_id != dataset_id: raise not_found()
    if export.status != ExportStatus.ready or not export.object_key: raise HTTPException(status_code=410, detail="Export is not available")
    if export.expires_at is None or export.expires_at <= now():
        deletion_succeeded = False
        try:
            expired_path = _safe_export_path(settings.export_root, settings.export_root / export.object_key)
            deletion_succeeded = _remove_export_artifact(expired_path, export.id)
        except Exception:
            logger.exception("failed to remove expired export during download", extra={"export_id": str(export.id)})
        if deletion_succeeded:
            export.status, export.expired_at = ExportStatus.expired, now()
            await session.commit()
        raise HTTPException(status_code=410, detail="Export has expired")
    path = _safe_export_path(settings.export_root, settings.export_root / export.object_key)
    root = settings.export_root.resolve()
    if root != path and root not in path.parents or not path.is_file(): raise HTTPException(status_code=410, detail="Export is not available")
    return FileResponse(path, media_type="application/jsonl", filename=f"{export.id}.jsonl")
