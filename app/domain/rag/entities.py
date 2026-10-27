from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass
class RagConfig:
    chat_model: str
    embedding_model: str
    retrieval_k: int
    rag_prompt: str


@dataclass
class ConfigurationEntry:
    id: UUID
    key: str
    value: str
