from fastapi import APIRouter, Depends, HTTPException

from app.application.rag.chat_service import ChatService, IngestService
from app.delivery.dependencies import get_optional_uow, require_rag_token
from app.delivery.schemas.rag import IngestRequest, QueryRequest, QueryResponse
from app.domain.unit_of_work import UnitOfWork
from app.infrastructure.rag.vector_store import create_chat_model, create_vector_store

router = APIRouter(tags=["rag"], dependencies=[Depends(require_rag_token)])


@router.post("/ingest", summary="Simpan potongan kode ke Vector DB")
async def ingest_code(
    req: IngestRequest,
    uow: UnitOfWork | None = Depends(get_optional_uow),
):
    try:
        await IngestService(
            uow, vector_store_factory=create_vector_store
        ).ingest(
            code_content=req.code_content,
            file_path=req.file_path,
            language=req.language,
        )
        return {
            "status": "success",
            "message": f"Berhasil menyimpan {req.file_path}",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/query", response_model=QueryResponse, summary="Tanya kode menggunakan RAG"
)
async def query_code(
    req: QueryRequest,
    uow: UnitOfWork | None = Depends(get_optional_uow),
):
    try:
        answer = await ChatService(
            uow,
            vector_store_factory=create_vector_store,
            chat_model_factory=create_chat_model,
        ).answer_question(req.question)
        return QueryResponse(answer=answer)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
