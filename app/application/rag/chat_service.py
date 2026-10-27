import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from app.core.exceptions import ChatUnavailable, InvalidInput
from app.domain.identifiers import new_uuid7
from app.domain.rag.entities import ChatMessage
from app.domain.unit_of_work import UnitOfWork

from .config_service import load_configuration


def format_documents(documents: Any) -> str:
    return "\n\n".join(
        f"File: {doc.metadata.get('source', 'Unknown')}\n{doc.page_content}"
        for doc in documents
    )


def build_rag_prompt(configuration: dict[str, str], context: str, question: str) -> str:
    return configuration["rag.prompt"].format(context=context, question=question)


@dataclass
class ChatRun:
    completion_id: str
    created: int
    model_name: str
    prompt: str
    chat_model: Any


class ChatService:
    def __init__(
        self,
        uow: UnitOfWork | None,
        *,
        vector_store_factory: Callable[[str], Any],
        chat_model_factory: Callable[[str], Any],
    ) -> None:
        self._uow = uow
        self._create_vector_store = vector_store_factory
        self._create_chat_model = chat_model_factory

    async def configuration(self) -> dict[str, str]:
        repository = self._uow.configurations if self._uow is not None else None
        return await load_configuration(repository)

    async def prepare_chat(
        self, messages: list[ChatMessage], requested_model: str | None
    ) -> ChatRun:
        configuration = await self.configuration()
        model_name = configuration["chat.model"]
        if requested_model is not None and requested_model != model_name:
            raise InvalidInput("Model is configured by the operator")

        question = messages[-1].content
        transcript = "\n".join(f"{m.role}: {m.content}" for m in messages)
        prompt = await self._retrieve_prompt(configuration, question, transcript)
        return ChatRun(
            completion_id=f"chatcmpl-{new_uuid7().hex}",
            created=int(time.time()),
            model_name=model_name,
            prompt=prompt,
            chat_model=self._create_chat_model(model_name),
        )

    async def answer_question(self, question: str) -> str:
        configuration = await self.configuration()
        prompt = await self._retrieve_prompt(configuration, question, question)
        try:
            return str(
                await self._create_chat_model(configuration["chat.model"]).ainvoke(prompt)
            )
        except Exception as exc:
            raise ChatUnavailable("Chat model is unavailable") from exc

    async def stream(self, run: ChatRun) -> AsyncIterator[str]:
        async for token in run.chat_model.astream(run.prompt):
            yield str(token)

    async def _retrieve_prompt(
        self, configuration: dict[str, str], question: str, transcript: str
    ) -> str:
        vector_store = self._create_vector_store(configuration["embedding.model"])
        try:
            documents = await asyncio.to_thread(
                vector_store.similarity_search,
                question,
                k=int(configuration["retrieval.k"]),
            )
        except Exception as exc:
            raise ChatUnavailable("Chat retrieval is unavailable") from exc
        return build_rag_prompt(
            configuration, format_documents(documents), transcript
        )


class IngestService:
    def __init__(
        self,
        uow: UnitOfWork | None,
        *,
        vector_store_factory: Callable[[str], Any],
    ) -> None:
        self._uow = uow
        self._create_vector_store = vector_store_factory

    async def ingest(
        self, *, code_content: str, file_path: str, language: str
    ) -> None:
        repository = self._uow.configurations if self._uow is not None else None
        if repository is not None:
            await repository.get("embedding.model", for_update=True)
        configuration = await load_configuration(repository)
        vector_store = self._create_vector_store(configuration["embedding.model"])
        await asyncio.to_thread(
            vector_store.add_texts,
            texts=[code_content],
            metadatas=[{"source": file_path, "language": language}],
        )
