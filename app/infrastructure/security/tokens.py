from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from app.core.config import Settings


def create_access_token(subject: UUID, settings: Settings) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    return jwt.encode(
        {"sub": str(subject), "exp": expires},
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_subject(token: str, settings: Settings) -> UUID:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    return UUID(str(payload["sub"]))
