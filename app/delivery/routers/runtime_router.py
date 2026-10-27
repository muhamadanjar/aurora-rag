import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.application.rag.chat_service import ChatService
from app.application.rag.runtime_config_service import RuntimeConfigService
from app.core.exceptions import ChatUnavailable
from app.core.exceptions import ResourceConflict
from app.delivery.dependencies import current_operator, get_uow
from app.delivery.schemas.rag import ChatCompletionRequest, ConfigurationUpdate
from app.domain.rag.entities import ChatMessage
from app.domain.unit_of_work import UnitOfWork
from app.infrastructure.rag.vector_store import (
    create_chat_model,
    create_vector_store,
    indexed_codebase_count,
    indexed_vector_count,
)
from app.infrastructure.codebase.index_store import CodebaseIndexStore, ProjectBusy

router = APIRouter(prefix="/api/v1", tags=["runtime configuration", "chat"])


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.get("/configuration")
async def list_configuration(
    uow: UnitOfWork = Depends(get_uow),
    actor=Depends(current_operator),
):
    entries = await RuntimeConfigService(
        uow, vector_count=lambda: max(indexed_vector_count(), indexed_codebase_count())
    ).list()
    return {"data": entries}


@router.put("/configuration/{key}")
async def update_configuration(
    key: str,
    body: ConfigurationUpdate,
    uow: UnitOfWork = Depends(get_uow),
    actor=Depends(current_operator),
):
    try:
        entry = await RuntimeConfigService(
            uow, vector_count=lambda: max(indexed_vector_count(), indexed_codebase_count()),
            model_lock_factory=CodebaseIndexStore().acquire_model_lock,
        ).update(key, body.value)
    except ProjectBusy as exc:
        raise ResourceConflict("embedding model operation already in progress") from exc
    return {"id": str(entry.id), "key": entry.key, "value": entry.value}


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    uow: UnitOfWork = Depends(get_uow),
    actor=Depends(current_operator),
):
    service = ChatService(
        uow,
        vector_store_factory=create_vector_store,
        chat_model_factory=create_chat_model,
    )
    messages = [
        ChatMessage(role=message.role, content=message.content)
        for message in body.messages
    ]
    run = await service.prepare_chat(messages, body.model)

    if not body.stream:
        try:
            content = await run.chat_model.ainvoke(run.prompt)
        except Exception as exc:
            raise ChatUnavailable("Chat model is unavailable") from exc
        return {
            "id": run.completion_id,
            "object": "chat.completion",
            "created": run.created,
            "model": run.model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": str(content)},
                    "finish_reason": "stop",
                }
            ],
        }

    async def event_stream():
        yield _event(
            {
                "id": run.completion_id,
                "object": "chat.completion.chunk",
                "created": run.created,
                "model": run.model_name,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            }
        )
        try:
            async for token in service.stream(run):
                yield _event(
                    {
                        "id": run.completion_id,
                        "object": "chat.completion.chunk",
                        "created": run.created,
                        "model": run.model_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": token},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
        except Exception:
            yield _event(
                {
                    "error": {
                        "message": "Chat model is unavailable",
                        "type": "server_error",
                    }
                }
            )
        else:
            yield _event(
                {
                    "id": run.completion_id,
                    "object": "chat.completion.chunk",
                    "created": run.created,
                    "model": run.model_name,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                }
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
