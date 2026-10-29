from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse

from app.application.dataset.export_service import (
    create_export,
    get_export,
    resolve_download_path,
)
from app.application.dataset.service import DatasetService
from app.core.config import Settings
from app.core.exceptions import DatasetNotFound, ResourceConflict
from app.delivery.dependencies import current_operator, get_uow, require_settings
from app.delivery.etag import entity_etag, parse_if_match
from app.delivery.schemas.dataset import (
    DatasetCreate,
    DatasetUpdate,
    ExampleCreate,
    ExportResponse,
    ReviewRequest,
    RevisionCreate,
)
from app.domain.unit_of_work import UnitOfWork

router = APIRouter()


# ── datasets ─────────────────────────────────────────────────────────────────


@router.post("/datasets", status_code=201)
async def create_dataset(
    body: DatasetCreate,
    response: Response,
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    dataset = await DatasetService(uow).create_dataset(
        title=body.title,
        description=body.description,
        format_type=body.format_type,
        actor=actor,
    )
    response.headers["ETag"] = entity_etag(dataset)
    return dataset


@router.get("/datasets")
async def list_datasets(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    datasets, total = await DatasetService(uow).list_datasets(
        page=page, per_page=per_page
    )
    return {
        "data": datasets,
        "meta": {
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": (total + per_page - 1) // per_page,
            }
        },
    }


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: UUID,
    response: Response,
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    dataset = await DatasetService(uow).get_dataset(dataset_id)
    if dataset is None:
        raise DatasetNotFound()
    response.headers["ETag"] = entity_etag(dataset)
    return dataset


@router.patch("/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: UUID,
    body: DatasetUpdate,
    response: Response,
    if_match: str | None = Header(default=None),
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    expected_version = parse_if_match(if_match)
    service = DatasetService(uow)
    dataset = await service.get_active_dataset(dataset_id, for_update=True)
    dataset = await service.update_dataset(
        dataset,
        body.model_dump(exclude_unset=True),
        actor,
        expected_version=expected_version,
    )
    response.headers["ETag"] = entity_etag(dataset)
    return dataset


@router.post("/datasets/{dataset_id}/archive", status_code=204)
async def archive_dataset(
    dataset_id: UUID,
    if_match: str | None = Header(default=None),
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    expected_version = parse_if_match(if_match)
    service = DatasetService(uow)
    dataset = await service.get_active_dataset(dataset_id, for_update=True)
    await service.archive_dataset(
        dataset, actor, expected_version=expected_version
    )


# ── examples ──────────────────────────────────────────────────────────────────


@router.post("/datasets/{dataset_id}/examples", status_code=201)
async def create_example(
    dataset_id: UUID,
    body: ExampleCreate,
    response: Response,
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    example, revision = await DatasetService(uow).create_example(
        dataset_id=dataset_id,
        content=body.content,
        system_prompt=body.system_prompt,
        schema_version=body.schema_version,
        actor=actor,
    )
    response.headers["ETag"] = entity_etag(example)
    response.headers["Revision-ETag"] = entity_etag(revision)
    return {"example": example, "revision": revision}


@router.get("/datasets/{dataset_id}/examples")
async def list_examples(
    dataset_id: UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    service = DatasetService(uow)
    await service.get_active_dataset(dataset_id)
    examples = await service.list_examples(
        dataset_id=dataset_id, page=page, per_page=per_page
    )
    return [
        {
            **example.__dict__,
            "current_revision": revision,
        }
        for example, revision in examples
    ]


# ── revisions ─────────────────────────────────────────────────────────────────


@router.post("/datasets/{dataset_id}/examples/{example_id}/revisions", status_code=201)
async def create_revision(
    dataset_id: UUID,
    example_id: UUID,
    body: RevisionCreate,
    response: Response,
    if_match: str | None = Header(default=None),
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    expected_version = parse_if_match(if_match)
    example, revision = await DatasetService(uow).create_revision(
        dataset_id=dataset_id,
        example_id=example_id,
        content=body.content,
        system_prompt=body.system_prompt,
        schema_version=body.schema_version,
        actor=actor,
        expected_example_version=expected_version,
    )
    response.headers["ETag"] = entity_etag(example)
    response.headers["Revision-ETag"] = entity_etag(revision)
    return revision


@router.post(
    "/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/validate"
)
async def validate_revision(
    dataset_id: UUID,
    example_id: UUID,
    revision_id: UUID,
    response: Response,
    if_match: str | None = Header(default=None),
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    expected_version = parse_if_match(if_match)
    service = DatasetService(uow)
    dataset = await service.get_active_dataset(dataset_id)
    revision, errors = await service.validate_revision(
        dataset_id=dataset_id,
        example_id=example_id,
        revision_id=revision_id,
        actor=actor,
        expected_revision_version=expected_version,
    )
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"code": f"INVALID_{dataset.format_type.upper()}", "errors": errors},
        )
    response.headers["ETag"] = entity_etag(revision)
    return revision


@router.post(
    "/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/review"
)
async def review_revision(
    dataset_id: UUID,
    example_id: UUID,
    revision_id: UUID,
    body: ReviewRequest,
    response: Response,
    if_match: str | None = Header(default=None),
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    expected_version = parse_if_match(if_match)
    revision, example = await DatasetService(uow).review_revision(
        dataset_id=dataset_id,
        example_id=example_id,
        revision_id=revision_id,
        approved=body.approved,
        review_note=body.review_note,
        actor=actor,
        expected_revision_version=expected_version,
    )
    response.headers["ETag"] = entity_etag(revision)
    response.headers["Example-ETag"] = entity_etag(example)
    return revision


@router.post("/datasets/{dataset_id}/examples/{example_id}/retire", status_code=204)
async def retire_example(
    dataset_id: UUID,
    example_id: UUID,
    response: Response,
    if_match: str | None = Header(default=None),
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    expected_version = parse_if_match(if_match)
    example = await DatasetService(uow).retire_example(
        dataset_id=dataset_id,
        example_id=example_id,
        actor=actor,
        expected_example_version=expected_version,
    )
    response.headers["ETag"] = entity_etag(example)


# ── exports ───────────────────────────────────────────────────────────────────


@router.post(
    "/datasets/{dataset_id}/exports",
    response_model=ExportResponse,
    status_code=201,
)
async def export_dataset(
    dataset_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
    settings: Settings = Depends(require_settings),
):
    dataset = await DatasetService(uow).get_active_dataset(
        dataset_id, for_update=True
    )
    try:
        return await create_export(uow.session, dataset, actor, settings)
    except ValueError as exc:
        raise ResourceConflict(str(exc)) from exc


@router.get(
    "/datasets/{dataset_id}/exports/{export_id}", response_model=ExportResponse
)
async def export_status(
    dataset_id: UUID,
    export_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
):
    return await get_export(uow.session, dataset_id, export_id)


@router.get("/datasets/{dataset_id}/exports/{export_id}/download")
async def download_export(
    dataset_id: UUID,
    export_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    actor: str = Depends(current_operator),
    settings: Settings = Depends(require_settings),
):
    path = await resolve_download_path(
        uow.session, dataset_id, export_id, settings
    )
    return FileResponse(
        path, media_type="application/jsonl", filename=f"{export_id}.jsonl"
    )
