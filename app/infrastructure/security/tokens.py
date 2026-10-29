from datetime import datetime, timedelta, timezone

from jose import jwt

from app.core.config import Settings


def create_access_token(subject: str, settings: Settings) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    return jwt.encode(
        {
            "sub": str(subject),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": expires,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_subject(token: str, settings: Settings) -> str:
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        options={"require_exp": True, "require_iss": True, "require_aud": True, "require_sub": True},
    )
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("JWT sub must be a non-empty string")
    return subject
