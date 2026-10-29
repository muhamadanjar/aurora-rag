import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from app.application.codebase.service import CodebaseService
from app.application.rag.config_service import load_configuration
from app.delivery.dependencies import get_optional_uow, require_codebase_token
from app.domain.unit_of_work import UnitOfWork
from app.delivery.schemas.codebase import (
    CodebaseIndexRequest, CodebaseProjectListResponse, CodebaseSearchRequest,
    CodebaseSearchResponse,
)
from app.infrastructure.codebase.index_store import (CodebaseIndexError, CodebaseIndexStore,
                                                       InvalidProject, ProjectBusy, RootNotAllowed)


router = APIRouter(
    prefix="/codebase",
    tags=["codebase"],
    dependencies=[Depends(require_codebase_token)],
)


def _service(store: CodebaseIndexStore | None = None) -> CodebaseService:
    return CodebaseService(store)


@router.post("/projects/{project_id}/index")
async def index_project(
    project_id: str,
    body: CodebaseIndexRequest,
    uow: UnitOfWork | None = Depends(get_optional_uow),
):
    store = CodebaseIndexStore()
    model_lock = None
    try:
        model_lock = await asyncio.to_thread(store.acquire_model_lock)
        configuration = await load_configuration(uow.configurations if uow else None)
        return await asyncio.to_thread(
            _service(store).index, project_id, body.path,
            embedding_model=configuration["embedding.model"], model_lock_held=True,
        )
    except ProjectBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidProject, RootNotAllowed) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CodebaseIndexError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail="Codebase indexing failed") from exc
    finally:
        if model_lock is not None:
            model_lock.release()


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str):
    try:
        await asyncio.to_thread(_service().delete, project_id)
    except ProjectBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidProject as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
