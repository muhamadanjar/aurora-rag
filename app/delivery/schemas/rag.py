from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.rag.config_service import MAX_CONFIGURATION_VALUE_LENGTH

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
        if sum(len(m.content) for m in self.messages) > MAX_CHAT_TRANSCRIPT_LENGTH:
            raise ValueError("Conversation is too long")
        return self


class IngestRequest(BaseModel):
    code_content: str
    file_path: str
    language: str


class QueryRequest(BaseModel):
    question: str


class QueryResponse(BaseModel):
    answer: str
