from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.core.config import Settings
from app.delivery.dependencies import current_operator, require_settings


@pytest.mark.asyncio
async def test_usermanagement_access_token_authenticates_as_configured_operator():
    operator_uuid = uuid4()
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@localhost/aurora",
        auth_username="dashboard-operator",
        auth_password_hash="$2b$12$placeholder",
        jwt_secret="shared-user-management-secret-at-least-32-chars",
        jwt_issuer="http://localhost:8070",
        jwt_audience="dashboard",
        operator_uuid=operator_uuid,
    )
    issuer = "http://localhost:8070"
    audiences = [
        "usermanagement-api",
        "backoffice",
        "payment-pos",
        "upload-api",
        "etl-api",
        "lms-system",
        "dashboard",
        "geoportal",
    ]
    app = FastAPI()
    app.dependency_overrides[require_settings] = lambda: settings

    @app.get("/datasets")
    async def list_datasets(actor=Depends(current_operator)):
        return {"actor": str(actor), "actor_type": type(actor).__name__}

    def issue_token(subject, *, token_issuer=issuer, token_audiences=audiences):
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "sub": subject,
                "username": subject,
                "user_id": str(uuid4()),
                "email": "operator@example.test",
                "iss": token_issuer,
                "aud": token_audiences,
                "exp": now + timedelta(minutes=5),
                "iat": int(now.timestamp()),
                "jti": str(uuid4()),
                "token_type": "access",
                "scopes": [],
            },
            settings.jwt_secret,
            algorithm="HS256",
        )

    token = issue_token(settings.auth_username)
    other_user_token = issue_token("another-user")
    wrong_audience_token = issue_token(
        settings.auth_username, token_audiences=["another-service"]
    )
    wrong_issuer_token = issue_token(
        settings.auth_username, token_issuer="https://untrusted.example"
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.get("/datasets", headers={"Authorization": f"Bearer {token}"})
        rejected = await client.get(
            "/datasets", headers={"Authorization": f"Bearer {other_user_token}"}
        )
        wrong_audience = await client.get(
            "/datasets", headers={"Authorization": f"Bearer {wrong_audience_token}"}
        )
        wrong_issuer = await client.get(
            "/datasets", headers={"Authorization": f"Bearer {wrong_issuer_token}"}
        )

    assert accepted.status_code == 200
    assert accepted.json() == {"actor": str(operator_uuid), "actor_type": "UUID"}
    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "Invalid operator"}
    assert wrong_audience.status_code == 401
    assert wrong_issuer.status_code == 401
