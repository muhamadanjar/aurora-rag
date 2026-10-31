import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ValidationError

from app.application.codebase.service import CodebaseService
from app.application.rag.config_service import load_configuration
from app.delivery.dependencies import get_optional_uow, get_uow, require_codebase_token
from app.domain.unit_of_work import UnitOfWork
from app.delivery.schemas.codebase import (
    CodebaseIndexRequest, CodebaseProjectListResponse, CodebaseSearchRequest,
    CodebaseSearchResponse, IndexJobAccepted, IndexJobResponse,
)
from app.infrastructure.codebase.index_store import (CodebaseIndexError, CodebaseIndexStore,
                                                       InvalidProject, ProjectBusy, RootNotAllowed)
from app.infrastructure.codebase.jobs import (
    ActiveJobConflict, enqueue, get_job, run_delete_with_project_lease,
)
from app.infrastructure.db.connection import engine
from app.infrastructure.db.models import IndexJobStatus
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("uvicorn.error")

router = APIRouter(
    prefix="/codebase",
    tags=["codebase"],
    dependencies=[Depends(require_codebase_token)],
)


def _service(store: CodebaseIndexStore | None = None) -> CodebaseService:
    return CodebaseService(store)


@router.post("/projects/{project_id}/index", status_code=status.HTTP_202_ACCEPTED,
             response_model=IndexJobAccepted)
async def index_project(
    project_id: str,
    body: CodebaseIndexRequest,
    response: Response,
    uow: UnitOfWork = Depends(get_uow),
):
    store = CodebaseIndexStore()
    try:
        store.validate_project_id(project_id)
        await asyncio.to_thread(store.validate_requested_path, body.path)
        job = await enqueue(uow.session, project_id, body.path)
    except ActiveJobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidProject, RootNotAllowed) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="Index job storage is unavailable") from exc
    status_url = f"/codebase/index-jobs/{job.id}"
    logger.info("codebase_index_job job_id=%s stage=queued status=queued", job.id)
    response.headers["Location"] = status_url
    return {"job_id": job.id, "project_id": job.project_id, "status": job.status.value,
            "status_url": status_url}


@router.get("/index-jobs/{job_id}", response_model=IndexJobResponse)
async def index_job_status(job_id: str, uow: UnitOfWork = Depends(get_uow)):
    from uuid import UUID
    try:
        job = await get_job(uow.session, UUID(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Index job not found") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Index job storage is unavailable") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Index job not found")
    result = None
    if job.status == IndexJobStatus.succeeded:
        result = {"indexed_chunks": job.indexed_chunks or 0, "skipped_files": job.skipped_files}
    error = None
    if job.status == IndexJobStatus.failed and job.error_code:
        error = {"code": job.error_code, "message": job.error_message or "Indexing failed"}
    return {"job_id": job.id, "project_id": job.project_id, "status": job.status.value,
            "stage": job.stage.value if job.stage else None,
            "progress": {"processed_chunks": job.processed_chunks,
                          "total_chunks": job.total_chunks, "skipped_files": job.skipped_files},
            "result": result, "error": error, "created_at": job.created_at,
            "started_at": job.started_at, "finished_at": job.finished_at}


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str):
    try:
        await run_delete_with_project_lease(
            project_id, lambda: _service().delete(project_id), engine().connect
        )
    except (ProjectBusy, ActiveJobConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidProject as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError, ValidationError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="Index job storage is unavailable") from exc


@router.get("/projects", response_model=CodebaseProjectListResponse)
async def list_projects():
    projects = await asyncio.to_thread(_service().projects)
    return {"projects": [{"project_id": project} for project in projects]}


@router.post("/search", response_model=CodebaseSearchResponse)
async def search_codebase(
    body: CodebaseSearchRequest,
    uow: UnitOfWork | None = Depends(get_optional_uow),
):
    try:
        configuration = await load_configuration(uow.configurations if uow else None)
        results = await asyncio.to_thread(
            _service().search, body.project_id, body.query, body.limit,
            embedding_model=configuration["embedding.model"],
        )
        return {"results": results}
    except ProjectBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidProject as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail="Codebase search failed") from exc
