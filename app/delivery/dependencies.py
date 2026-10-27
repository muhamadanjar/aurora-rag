from collections.abc import AsyncGenerator
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.domain.unit_of_work import UnitOfWork
from app.infrastructure.db.connection import create_unit_of_work
from app.infrastructure.security.tokens import decode_subject

bearer = HTTPBearer(auto_error=False)


def require_settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Dataset management authentication is not configured",
        ) from exc


async def get_uow() -> AsyncGenerator[UnitOfWork, None]:
    try:
        uow = create_unit_of_work()
    except ValidationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Dataset management database is not configured",
        ) from exc
    async with uow:
        yield uow


async def get_optional_uow() -> AsyncGenerator[UnitOfWork | None, None]:
    try:
        uow = create_unit_of_work()
    except ValidationError:
        yield None
        return
    async with uow:
        yield uow


async def current_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(require_settings),
) -> UUID:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required"
        )
    try:
        subject = decode_subject(credentials.credentials, settings)
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc
    if subject != settings.operator_uuid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid operator"
        )
    return subject
