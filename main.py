import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dataset_management.db import SessionLocal, get_optional_session
from dataset_management.models import Configuration
from dataset_management.rag import build_rag_prompt, create_chat_model, create_vector_store, format_docs
from dataset_management.routes import router as dataset_router
from dataset_management.runtime_config import load_configuration
from dataset_management.runtime_routes import router as runtime_router
from dataset_management.service import cleanup_loop
from dataset_management.settings import get_settings

CHAT_COMPLETIONS_PATH = "/api/v1/chat/completions"
MAX_CHAT_REQUEST_BODY_BYTES = 1_048_576


class ChatRequestBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != CHAT_COMPLETIONS_PATH
        ):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=413,
            content={"detail": "Chat request body is too large"},
        )
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_CHAT_REQUEST_BODY_BYTES:
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        request_body = bytes(body)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": request_body, "more_body": False}

        await self.app(scope, replay_receive, send)


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
    title="Aurota RAG API", 
    description="Microservice API untuk RAG repositori kode",
    lifespan=lifespan,
)
app.add_middleware(ChatRequestBodyLimitMiddleware)
app.include_router(dataset_router)
app.include_router(runtime_router)


# ---------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------
class IngestRequest(BaseModel):
    code_content: str
    file_path: str
    language: str

class QueryRequest(BaseModel):
    question: str

class QueryResponse(BaseModel):
    answer: str

# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------
@app.post("/ingest", summary="Simpan potongan kode ke Vector DB")
async def ingest_code(
    req: IngestRequest,
    session: AsyncSession | None = Depends(get_optional_session),
):
    try:
        if session is None:
            configuration = await load_configuration(None)
            vector_store = create_vector_store(configuration["embedding.model"])
            await asyncio.to_thread(
                vector_store.add_texts,
                texts=[req.code_content],
                metadatas=[{"source": req.file_path, "language": req.language}],
            )
        else:
            async with session.begin():
                await session.scalar(
                    select(Configuration)
                    .where(Configuration.key == "embedding.model")
                    .with_for_update()
                )
                configuration = await load_configuration(session)
                vector_store = create_vector_store(configuration["embedding.model"])
                await asyncio.to_thread(
                    vector_store.add_texts,
                    texts=[req.code_content],
                    metadatas=[{"source": req.file_path, "language": req.language}],
                )
        return {"status": "success", "message": f"Berhasil menyimpan {req.file_path}"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/query", response_model=QueryResponse, summary="Tanya kode menggunakan RAG")
async def query_code(
    req: QueryRequest,
    session: AsyncSession | None = Depends(get_optional_session),
):
    try:
        configuration = await load_configuration(session)
        vector_store = create_vector_store(configuration["embedding.model"])
        documents = await asyncio.to_thread(
            vector_store.similarity_search,
            req.question,
            k=int(configuration["retrieval.k"]),
        )
        prompt = build_rag_prompt(configuration, format_docs(documents), req.question)
        response_text = await create_chat_model(configuration["chat.model"]).ainvoke(prompt)
        return QueryResponse(answer=str(response_text))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
