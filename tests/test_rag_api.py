import base64
import hashlib
import hmac

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.delivery.dependencies import get_optional_uow
from app.delivery.routers import rag_router
from app.main import app


@pytest_asyncio.fixture
async def client(monkeypatch):
    async def no_uow():
        yield None

    monkeypatch.setenv("AURORA_RAG_TOKEN", "rag-test-token")
    previous_override = app.dependency_overrides.get(get_optional_uow)
    app.dependency_overrides[get_optional_uow] = no_uow
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as value:
            yield value
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_optional_uow, None)
        else:
            app.dependency_overrides[get_optional_uow] = previous_override


@pytest.mark.asyncio
async def test_rag_endpoints_fail_closed_and_require_the_rag_bearer(client, monkeypatch):
    monkeypatch.delenv("AURORA_RAG_TOKEN")
    unconfigured = await client.post("/query", json={"question": "question"})
    assert unconfigured.status_code == 503

    monkeypatch.setenv("AURORA_RAG_TOKEN", "rag-test-token")
    missing_token = await client.post("/query", json={"question": "question"})
    assert missing_token.status_code == 401
    management_secret = "management-signing-secret"
    header = base64.urlsafe_b64encode(
        b'{"alg":"HS256","typ":"JWT"}'
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        b'{"sub":"00000000-0000-0000-0000-000000000001","exp":4102444800}'
    ).rstrip(b"=").decode()
    signing_input = f"{header}.{payload}".encode()
    signature = base64.urlsafe_b64encode(
        hmac.new(management_secret.encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    management_token = f"{signing_input.decode()}.{signature}"
    rejected_management_token = await client.post(
        "/query",
        headers={"Authorization": f"Bearer {management_token}"},
        json={"question": "question"},
    )
    assert rejected_management_token.status_code == 401
    unauthorized_ingest = await client.post(
        "/ingest",
        headers={"Authorization": f"Bearer {management_token}"},
        json={"code_content": "sample", "file_path": "sample.py", "language": "python"},
    )
    assert unauthorized_ingest.status_code == 401

    class StubChatService:
        def __init__(self, *args, **kwargs):
            pass

        async def answer_question(self, _question):
            return "authorized"

    monkeypatch.setattr(rag_router, "ChatService", StubChatService)
    accepted = await client.post(
        "/query",
        headers={"Authorization": "Bearer rag-test-token"},
        json={"question": "question"},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"answer": "authorized"}
