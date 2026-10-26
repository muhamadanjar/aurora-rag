from collections import deque
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import UUID

import anyio
import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from .settings import Settings, get_settings

bearer = HTTPBearer(auto_error=False)
_failed_logins: dict[str, deque[float]] = {}
_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_MAX_FAILURES = 5
_LOGIN_MAX_KEYS = 1024


async def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bool(password_hash) and await anyio.to_thread.run_sync(bcrypt.checkpw, password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


def create_token(subject: UUID, settings: Settings) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    return jwt.encode({"sub": str(subject), "exp": expires}, settings.jwt_secret, algorithm="HS256")


def reserve_login_attempt(key: str) -> bool:
    now_value = monotonic()
    attempts = _failed_logins.get(key)
    if attempts is not None:
        while attempts and now_value - attempts[0] > _LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if not attempts:
            _failed_logins.pop(key, None)
            attempts = None
        elif len(attempts) >= _LOGIN_MAX_FAILURES:
            return False
    if attempts is None:
        if len(_failed_logins) >= _LOGIN_MAX_KEYS:
            oldest_key = min(_failed_logins, key=lambda item: _failed_logins[item][0])
            _failed_logins.pop(oldest_key)
        attempts = deque()
        _failed_logins[key] = attempts
    attempts.append(now_value)
    return True


def clear_failed_logins(key: str) -> None:
    _failed_logins.pop(key, None)


def require_settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        raise HTTPException(status_code=503, detail="Dataset management authentication is not configured") from exc


async def current_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(require_settings),
) -> UUID:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
        subject = UUID(str(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    if subject != settings.operator_uuid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid operator")
    return subject
