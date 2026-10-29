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
    system_prompt: str | None = Field(default=None, max_length=MAX_CHAT_MESSAGE_LENGTH)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)

    @model_validator(mode="after")
    def validate_conversation(self):
        if self.messages[-1].role != "user":
            raise ValueError("The last message must have role user")
        transcript_length = sum(len(m.content) for m in self.messages)
        if transcript_length > MAX_CHAT_TRANSCRIPT_LENGTH:
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
