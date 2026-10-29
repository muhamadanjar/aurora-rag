from uuid import uuid4

import pytest
from jose import JWTError

from app.core.config import Settings
from app.infrastructure.security.tokens import create_access_token, decode_subject


def test_access_token_round_trips_and_rejects_tampering():
    subject = "operator"
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@localhost/aurora",
        auth_username="operator",
        auth_password_hash="$2b$12$placeholder",
        jwt_secret="token-signing-secret-with-at-least-32-chars",
        operator_uuid=uuid4(),
    )

    token = create_access_token(subject, settings)

    assert decode_subject(token, settings) == subject
    with pytest.raises(JWTError):
        decode_subject(f"{token}tampered", settings)
