import asyncio
import json
import logging
import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import current_operator
from .db import get_session
from .models import Configuration
from .rag import build_rag_prompt, create_chat_model, create_vector_store, format_docs
from .runtime_config import (
    ALLOWED_CONFIGURATION_KEYS,
    DEFAULT_CONFIGURATION,
    MAX_CONFIGURATION_VALUE_LENGTH,
    indexed_vector_count,
    load_configuration,
    validate_configuration_value,
)

router = APIRouter(prefix="/api/v1", tags=["runtime configuration", "chat"])
logger = logging.getLogger(__name__)
MAX_CHAT_MESSAGES = 32
MAX_CHAT_MESSAGE_LENGTH = 16_384
MAX_CHAT_TRANSCRIPT_LENGTH = 100_000


class ConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=MAX_CONFIGURATION_VALUE_LENGTH)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LENGTH)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_CHAT_MESSAGES)
    model: str | None = Field(default=None, max_length=128)
    stream: bool = True

    @model_validator(mode="after")
    def validate_conversation(self):
        if self.messages[-1].role != "user":
            raise ValueError("The last message must have role user")
        if sum(len(message.content) for message in self.messages) > MAX_CHAT_TRANSCRIPT_LENGTH:
            raise ValueError("Conversation is too long")
        return self


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.get("/configuration")
async def list_configuration(
    session: AsyncSession = Depends(get_session, scope="function"),
    actor=Depends(current_operator),
):
    rows = await session.scalars(
        select(Configuration)
        .where(Configuration.key.in_(ALLOWED_CONFIGURATION_KEYS))
        .order_by(Configuration.key)
    )
    return {"data": [row.model_dump() for row in rows]}


@router.put("/configuration/{key}")
async def update_configuration(
    key: str,
    body: ConfigurationUpdate,
    session: AsyncSession = Depends(get_session, scope="function"),
    actor=Depends(current_operator),
):
    try:
        value = validate_configuration_value(key, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async with session.begin():
        row = await session.scalar(
            select(Configuration)
            .where(Configuration.key == key)
            .with_for_update()
        )
        current_value = row.value if row is not None else DEFAULT_CONFIGURATION[key]
        if key == "embedding.model" and current_value != value:
            if await asyncio.to_thread(indexed_vector_count) > 0:
                raise HTTPException(
                    status_code=409,
                    detail="embedding.model cannot change after Chroma contains vectors",
                )
        if row is None:
            row = Configuration(id=uuid4(), key=key, value=value)
            session.add(row)
        else:
            row.value = value
        await session.flush()
        response = {"id": str(row.id), "key": row.key, "value": row.value}
    return response


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    session: AsyncSession = Depends(get_session, scope="function"),
    actor=Depends(current_operator),
):
    configuration = await load_configuration(session)
    if body.model is not None and body.model != configuration["chat.model"]:
        raise HTTPException(status_code=422, detail="Model is configured by the operator")
    question = body.messages[-1].content
    transcript = "\n".join(
        f"{message.role}: {message.content}" for message in body.messages
    )
    vector_store = create_vector_store(configuration["embedding.model"])
    try:
        documents = await asyncio.to_thread(
            vector_store.similarity_search,
            question,
            k=int(configuration["retrieval.k"]),
        )
        prompt = build_rag_prompt(configuration, format_docs(documents), transcript)
    except Exception as exc:
        logger.warning("Chat retrieval failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Chat retrieval is unavailable") from exc

    model_name = configuration["chat.model"]
    chat_model = create_chat_model(model_name)
    completion_id = f"chatcmpl-{uuid4().hex}"
    created = int(time.time())

    if not body.stream:
        try:
            content = await chat_model.ainvoke(prompt)
        except Exception as exc:
            logger.warning("Chat generation failed: %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Chat model is unavailable") from exc
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
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
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
        try:
            async for token in chat_model.astream(prompt):
                yield _event(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{"index": 0, "delta": {"content": str(token)}, "finish_reason": None}],
                    }
                )
        except Exception as exc:
            logger.warning("Streaming chat generation failed: %s", type(exc).__name__)
            yield _event({"error": {"message": "Chat model is unavailable", "type": "server_error"}})
        else:
            yield _event(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
