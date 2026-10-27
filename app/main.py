import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.application.dataset.export_service import cleanup_loop
from app.core.config import get_settings
from app.core.exceptions import (
    ChatUnavailable,
    ConfigurationInvalid,
    DatasetNotFound,
    ExportExpired,
    ExportNotFound,
    ExportNotReady,
    InvalidInput,
    PreconditionFailed,
    PreconditionRequired,
    ResourceConflict,
)
from app.delivery.middleware import ChatRequestBodyLimitMiddleware
from app.delivery.routers.auth_router import router as auth_router
from app.delivery.routers.codebase_router import router as codebase_router
from app.delivery.routers.dataset_router import router as dataset_router
from app.delivery.routers.rag_router import router as rag_router
from app.delivery.routers.runtime_router import router as runtime_router
from app.infrastructure.db.connection import SessionLocal


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        settings = get_settings()
    except ValidationError:
        yield
        return
    cleanup_task = asyncio.create_task(cleanup_loop(SessionLocal, settings))
    try:
        yield
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)


app = FastAPI(
    title="Aurora RAG API",
    description="Microservice API untuk RAG repositori kode",
    lifespan=lifespan,
)
app.add_middleware(ChatRequestBodyLimitMiddleware)
app.include_router(auth_router)
app.include_router(dataset_router)
app.include_router(runtime_router)
app.include_router(rag_router)
app.include_router(codebase_router)


_STATUS_BY_EXCEPTION = {
    DatasetNotFound: (404, "Resource not found"),
    ExportNotFound: (404, "Resource not found"),
    ExportNotReady: (410, "Export is not available"),
    ExportExpired: (410, "Export has expired"),
    PreconditionFailed: (412, "Resource has changed"),
    PreconditionRequired: (428, "If-Match header is required"),
}


def _exception_handler(status_code: int, detail: str):
    async def handler(_: Request, __: Exception):
        return JSONResponse(status_code=status_code, content={"detail": detail})

    return handler


for _exc_type, (_status, _detail) in _STATUS_BY_EXCEPTION.items():
    app.add_exception_handler(_exc_type, _exception_handler(_status, _detail))


@app.exception_handler(ResourceConflict)
async def handle_conflict(_: Request, exc: ResourceConflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ConfigurationInvalid)
async def handle_configuration_invalid(_: Request, exc: ConfigurationInvalid):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(InvalidInput)
async def handle_invalid_input(_: Request, exc: InvalidInput):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(ChatUnavailable)
async def handle_chat_unavailable(_: Request, exc: ChatUnavailable):
    return JSONResponse(status_code=502, content={"detail": str(exc)})
