from functools import lru_cache
from pathlib import Path
from uuid import UUID

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    auth_username: str
    auth_password_hash: str
    jwt_secret: str
    operator_uuid: UUID
    jwt_issuer: str = "http://localhost:8000"
    jwt_audience: str = "dashboard"
    export_root: Path = Path("./data/exports")
    export_retention_days: int = 30
    cleanup_interval_seconds: int = 3600

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: str) -> str:
        if len(value) < 32 or value in {"change-me", "replace-with-a-long-random-secret"}:
            raise ValueError("JWT_SECRET must contain at least 32 non-placeholder characters")
        return value

    @field_validator("auth_username")
    @classmethod
    def validate_auth_config(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("authentication configuration is required")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DATABASE_URL is required")
        return value

    @field_validator("auth_password_hash")
    @classmethod
    def validate_password_hash(cls, value: str) -> str:
        if not value.startswith(("$2a$", "$2b$", "$2y$")):
            raise ValueError("AUTH_PASSWORD_HASH must be a bcrypt hash")
        return value


class CodebaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aurora_codebase_token: str = ""
    aurora_codebase_roots: str = ""
    aurora_codebase_index_root: Path = Path("./data/codebase_index")
    aurora_codebase_embedding_model: str | None = None


@lru_cache
def get_codebase_settings() -> CodebaseSettings:
    return CodebaseSettings()


@lru_cache
def get_settings() -> Settings:
    return Settings()
