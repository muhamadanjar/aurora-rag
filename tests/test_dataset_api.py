"""API behavior coverage against the actual Alembic PostgreSQL schema."""
import asyncio
import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import bcrypt
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from anyio import to_thread
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.db import connection as db
from app.application.auth.service import clear_failed_logins
from app.infrastructure.db.models import DatasetExport, ExportStatus, now
from app.application.dataset.export_service import cleanup_expired_exports, cleanup_orphaned_export_files, reconcile_stale_exports
from app.core.config import get_settings
from app.main import app


@pytest.fixture
def database_url():
    return os.environ["TEST_DATABASE_URL"]


@pytest_asyncio.fixture
async def client(tmp_path, database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode())
    monkeypatch.setenv("JWT_SECRET", "test-secret-012345678901234567890123456789")
    monkeypatch.setenv("OPERATOR_UUID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("EXPORT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    db.session_factory.cache_clear()

    alembic_config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    await to_thread.run_sync(lambda: command.downgrade(alembic_config, "base"))
    await to_thread.run_sync(lambda: command.upgrade(alembic_config, "head"))

    engine = create_async_engine(database_url)
    previous_session_local = db.SessionLocal
    db.SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=db.AsyncSession)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
            yield value
    finally:
        db.SessionLocal = previous_session_local
        await engine.dispose()
        db.session_factory.cache_clear()
        get_settings.cache_clear()


async def login(client):
    response = await client.post("/auth/token", data={"username": "operator", "password": "secret"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def create_valid_revision(client, headers, dataset_id, *, system_prompt=None):
    payload = {"content": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    example = await client.post(f"/datasets/{dataset_id}/examples", headers=headers, json=payload)
    example_id = example.json()["example"]["id"]
    revision_id = example.json()["revision"]["id"]
    validated = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/validate",
        headers={**headers, "If-Match": example.headers["revision-etag"]},
    )
    reviewed = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/review",
        headers={**headers, "If-Match": validated.headers["etag"]},
        json={"approved": True},
    )
    assert reviewed.status_code == 200
    return example_id, revision_id


@pytest.mark.asyncio
async def test_login_throttles_concurrent_bad_credentials(client):
    clear_failed_logins("127.0.0.1")
    try:
        responses = await asyncio.gather(
            *(
                client.post("/auth/token", data={"username": "operator", "password": "wrong"})
                for _ in range(10)
            )
        )
        assert sum(response.status_code == 401 for response in responses) == 5
        blocked = [response for response in responses if response.status_code == 429]
        assert len(blocked) == 5
        assert all(response.headers["Retry-After"] == "60" for response in blocked)
    finally:
        clear_failed_logins("127.0.0.1")


@pytest.mark.asyncio
async def test_dataset_routes_require_bearer(client):
    assert (await client.get("/datasets")).status_code == 401
    assert (await client.post("/datasets", json={"title": "private"})).status_code == 401


@pytest.mark.asyncio
async def test_dataset_version_etag_duplicate_titles_and_null_title(client):
    headers = await login(client)
    first = await client.post("/datasets", headers=headers, json={"title": "repeat"})
    second = await client.post("/datasets", headers=headers, json={"title": "repeat"})
    assert first.status_code == second.status_code == 201
    assert (await client.post("/datasets", headers=headers, json={"title": "   "})).status_code == 422
    dataset_id = first.json()["id"]
    assert first.headers["etag"] == '"1"'
    updated = await client.patch(
        f"/datasets/{dataset_id}",
        headers={**headers, "If-Match": '"1"'},
        json={"description": "changed"},
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"2"'
    assert (await client.patch(f"/datasets/{dataset_id}", headers={**headers, "If-Match": '"1"'}, json={"description": "stale"})).status_code == 412
    assert (await client.post(f"/datasets/{dataset_id}/archive", headers={**headers, "If-Match": '"1"'})).status_code == 412
    assert (await client.patch(f"/datasets/{dataset_id}", headers={**headers, "If-Match": '"2"'}, json={"title": None})).status_code == 422
    assert (await client.post(f"/datasets/{dataset_id}/archive", headers={**headers, "If-Match": '"2"'})).status_code == 204
    archived = await client.get(f"/datasets/{dataset_id}", headers=headers)
    assert archived.headers["etag"] == '"3"'



@pytest.mark.asyncio
async def test_format_type_is_constrained_to_chatml(client):
    headers = await login(client)
    invalid = await client.post("/datasets", headers=headers, json={"title": "unsupported", "format_type": "alpaca"})
    assert invalid.status_code == 422
    dataset = await client.post("/datasets", headers=headers, json={"title": "chatml-only"})
    async with db.SessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE training_datasets SET format_type = 'alpaca' WHERE id = :dataset_id"),
                {"dataset_id": UUID(dataset.json()["id"])},
            )

@pytest.mark.asyncio
async def test_revision_creation_and_validation_review_gating(client):
    headers = await login(client)
    dataset = await client.post("/datasets", headers=headers, json={"title": "support"})
    dataset_id = dataset.json()["id"]
    example = await client.post(f"/datasets/{dataset_id}/examples", headers=headers, json={"content": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]})
    example_id = example.json()["example"]["id"]
    revision = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions",
        headers={**headers, "If-Match": example.headers["etag"]},
        json={"content": [{"role": "user", "content": "new"}, {"role": "assistant", "content": "reply"}]},
    )
    assert revision.status_code == 201
    assert revision.headers["etag"] == '"2"'
    revision_id = revision.json()["id"]
    assert (await client.post(f"/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/review", headers=headers, json={"approved": True})).status_code == 428
    assert (await client.post(f"/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/review", headers={**headers, "If-Match": revision.headers["revision-etag"]}, json={"approved": True})).status_code == 409
    validated = await client.post(f"/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/validate", headers={**headers, "If-Match": revision.headers["revision-etag"]})
    assert validated.status_code == 200
    reviewed = await client.post(f"/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/review", headers={**headers, "If-Match": validated.headers["etag"]}, json={"approved": True, "review_note": "approved after validation"})
    assert reviewed.status_code == 200
    assert reviewed.json()["review_note"] == "approved after validation"



@pytest.mark.asyncio
async def test_approving_older_revision_advances_example_etag(client):
    headers = await login(client)
    dataset = await client.post("/datasets", headers=headers, json={"title": "etag pointer"})
    dataset_id = dataset.json()["id"]
    first = await client.post(
        f"/datasets/{dataset_id}/examples",
        headers=headers,
        json={"content": [{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer"}]},
    )
    example_id = first.json()["example"]["id"]
    first_id = first.json()["revision"]["id"]
    first_validated = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions/{first_id}/validate",
        headers={**headers, "If-Match": first.headers["revision-etag"]},
    )
    first_reviewed = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions/{first_id}/review",
        headers={**headers, "If-Match": first_validated.headers["etag"]},
        json={"approved": True},
    )
    second = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions",
        headers={**headers, "If-Match": first_reviewed.headers["example-etag"]},
        json={"content": [{"role": "user", "content": "second"}, {"role": "assistant", "content": "answer"}]},
    )
    second_id = second.json()["id"]
    second_validated = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions/{second_id}/validate",
        headers={**headers, "If-Match": second.headers["revision-etag"]},
    )
    assert second_validated.status_code == 200

    switched_back = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions/{first_id}/review",
        headers={**headers, "If-Match": first_reviewed.headers["etag"]},
        json={"approved": True},
    )
    assert switched_back.status_code == 200
    assert switched_back.headers["example-etag"] == '"3"'
    stale = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions",
        headers={**headers, "If-Match": second.headers["etag"]},
        json={"content": [{"role": "user", "content": "stale"}, {"role": "assistant", "content": "answer"}]},
    )
    assert stale.status_code == 412
    current = await client.post(
        f"/datasets/{dataset_id}/examples/{example_id}/revisions",
        headers={**headers, "If-Match": switched_back.headers["example-etag"]},
        json={"content": [{"role": "user", "content": "third"}, {"role": "assistant", "content": "answer"}]},
    )
    assert current.status_code == 201

@pytest.mark.asyncio
async def test_export_exact_jsonl_manifest_checksum_and_expiry(client):
    headers = await login(client)
    dataset = await client.post("/datasets", headers=headers, json={"title": "exportable"})
    dataset_id = dataset.json()["id"]
    _, revision_id = await create_valid_revision(client, headers, dataset_id, system_prompt="Answer concisely")
    exported = await client.post(f"/datasets/{dataset_id}/exports", headers=headers)
    assert exported.status_code == 201
    export_payload = exported.json()
    assert export_payload["manifest"]["dataset_id"] == dataset_id
    assert export_payload["manifest"]["version"] == 1
    assert export_payload["manifest"]["item_count"] == 1
    assert export_payload["manifest"]["format_type"] == "chatml"
    assert export_payload["manifest"]["serialization"] == "jsonl"
    assert export_payload["manifest"]["export_schema_version"] == "1"
    assert export_payload["manifest"]["chatml"] is True
    assert export_payload["manifest"]["revision_ids"] == [revision_id]
    download = await client.get(f"/datasets/{dataset_id}/exports/{export_payload['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.content.endswith(b"\n")
    assert export_payload["checksum_sha256"] == hashlib.sha256(download.content).hexdigest()
    assert json.loads(download.text) == {"messages": [{"role": "system", "content": "Answer concisely"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]}

    async with db.SessionLocal() as session:
        stored = await session.get(DatasetExport, export_payload["id"])
        stored.expires_at = now()
        await session.commit()
    assert (await client.get(f"/datasets/{dataset_id}/exports/{export_payload['id']}/download", headers=headers)).status_code == 410
    status = await client.get(f"/datasets/{dataset_id}/exports/{export_payload['id']}", headers=headers)
    assert status.json()["status"] == "expired"


@pytest.mark.asyncio
async def test_expiration_cleanup_retains_metadata(client):
    headers = await login(client)
    dataset = await client.post("/datasets", headers=headers, json={"title": "cleanup"})
    dataset_id = dataset.json()["id"]
    await create_valid_revision(client, headers, dataset_id)
    export = await client.post(f"/datasets/{dataset_id}/exports", headers=headers)
    async with db.SessionLocal() as session:
        stored = await session.get(DatasetExport, export.json()["id"])
        stored.expires_at = now()
        expired_file = Path(get_settings().export_root) / stored.object_key
        await session.commit()
        await cleanup_expired_exports(session, get_settings())
        assert stored.status == ExportStatus.expired
        assert stored.expired_at is not None
        assert not expired_file.exists()



@pytest.mark.asyncio
async def test_stale_pending_reconciliation_removes_files(client, tmp_path):
    headers = await login(client)
    dataset = await client.post("/datasets", headers=headers, json={"title": "interrupted"})
    export = DatasetExport(
        dataset_id=UUID(dataset.json()["id"]),
        requested_by=get_settings().operator_uuid,
        status=ExportStatus.pending,
        manifest={"status": "pending"},
        created_at=now() - timedelta(days=2),
    )
    async with db.SessionLocal() as session:
        session.add(export)
        await session.commit()
        await session.refresh(export)
        final_file = tmp_path / f"{export.id}.jsonl"
        temporary_file = tmp_path / f".{export.id}.partial.tmp"
        final_file.write_text("orphan")
        temporary_file.write_text("partial")
        count = await reconcile_stale_exports(session, get_settings())
        assert count == 1
        assert export.status == ExportStatus.failed
    assert not final_file.exists()
    assert not temporary_file.exists()


@pytest.mark.asyncio
async def test_stale_reconciliation_skips_locked_export(client, tmp_path):
    headers = await login(client)
    dataset = await client.post("/datasets", headers=headers, json={"title": "active export lock"})
    export = DatasetExport(
        dataset_id=UUID(dataset.json()["id"]),
        requested_by=get_settings().operator_uuid,
        status=ExportStatus.pending,
        manifest={"status": "pending"},
        created_at=now() - timedelta(days=2),
    )
    async with db.SessionLocal() as creator:
        creator.add(export)
        await creator.commit()
        await creator.refresh(export)
    final_file = tmp_path / f"{export.id}.jsonl"
    final_file.write_text("still processing")

    lock_session = db.SessionLocal()
    try:
        await lock_session.begin()
        await lock_session.scalar(select(DatasetExport).where(DatasetExport.id == export.id).with_for_update())
        async with db.SessionLocal() as cleanup_session:
            assert await reconcile_stale_exports(cleanup_session, get_settings()) == 0
        assert final_file.exists()
        await lock_session.rollback()
    finally:
        await lock_session.close()
    async with db.SessionLocal() as cleanup_session:
        assert await reconcile_stale_exports(cleanup_session, get_settings()) == 1
    assert not final_file.exists()

@pytest.mark.asyncio
async def test_invalid_chatml_and_empty_export_are_rejected(client):
    headers = await login(client)
    dataset = await client.post("/datasets", headers=headers, json={"title": "invalid"})
    dataset_id = dataset.json()["id"]
    example = await client.post(f"/datasets/{dataset_id}/examples", headers=headers, json={"content": [{"role": "assistant", "content": "first"}]})
    revision_id = example.json()["revision"]["id"]
    invalid = await client.post(f"/datasets/{dataset_id}/examples/{example.json()['example']['id']}/revisions/{revision_id}/validate", headers={**headers, "If-Match": example.headers["revision-etag"]})
    assert invalid.status_code == 422
    empty = await client.post(f"/datasets/{dataset_id}/exports", headers=headers)
    assert empty.status_code == 409



@pytest.mark.asyncio
async def test_export_files_expire_after_database_downgrade(client, tmp_path):
    headers = await login(client)
    dataset = await client.post("/datasets", headers=headers, json={"title": "retained export"})
    dataset_id = dataset.json()["id"]
    await create_valid_revision(client, headers, dataset_id)
    exported = await client.post(f"/datasets/{dataset_id}/exports", headers=headers)
    assert exported.status_code == 201
    export_path = tmp_path / exported.json()["object_key"]
    assert export_path.exists()

    old_timestamp = time.time() - timedelta(days=31).total_seconds()
    os.utime(export_path, (old_timestamp, old_timestamp))
    stale_temp = tmp_path / f".{uuid4()}.partial.tmp"
    stale_temp.write_text("partial")
    os.utime(stale_temp, (old_timestamp, old_timestamp))
    unrelated_file = tmp_path / "notes.jsonl"
    unrelated_file.write_text("not an export")
    os.utime(unrelated_file, (old_timestamp, old_timestamp))

    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    await to_thread.run_sync(lambda: command.downgrade(config, "base"))
    assert export_path.exists()

    assert cleanup_orphaned_export_files(get_settings(), include_temporary=True) == 2
    assert not export_path.exists()
    assert not stale_temp.exists()
    assert unrelated_file.exists()
@pytest.mark.asyncio
async def test_configuration_api_authenticates_and_persists_runtime_values(client):
    unauthorized = await client.get("/api/v1/configuration")
    assert unauthorized.status_code == 401

    headers = await login(client)
    initial = await client.get("/api/v1/configuration", headers=headers)
    assert initial.status_code == 200
    values = {item["key"]: item["value"] for item in initial.json()["data"]}
    assert values["chat.model"] == "llama3"
    assert values["embedding.model"] == "nomic-embed-text"
    assert values["retrieval.k"] == "4"
    assert "{context}" in values["rag.prompt"]
    assert "{question}" in values["rag.prompt"]

    updated = await client.put(
        "/api/v1/configuration/chat.model",
        headers=headers,
        json={"value": "llama3.1"},
    )
    assert updated.status_code == 200

    refreshed = await client.get("/api/v1/configuration", headers=headers)
    assert refreshed.json()["data"]
    refreshed_values = {item["key"]: item["value"] for item in refreshed.json()["data"]}
    assert refreshed_values["chat.model"] == "llama3.1"

@pytest.mark.asyncio
async def test_configuration_api_rejects_unknown_keys_and_invalid_retrieval_k(client):
    headers = await login(client)
    unknown = await client.put(
        "/api/v1/configuration/private.secret",
        headers=headers,
        json={"value": "not-stored"},
    )
    invalid_k = await client.put(
        "/api/v1/configuration/retrieval.k",
        headers=headers,
        json={"value": "0"},
    )
    assert unknown.status_code == 422
    assert invalid_k.status_code == 422
    invalid_prompt = await client.put(
        "/api/v1/configuration/rag.prompt",
        headers=headers,
        json={"value": "Answer this: {question}"},
    )
    assert invalid_prompt.status_code == 422
    formatted_prompt = await client.put(
        "/api/v1/configuration/rag.prompt",
        headers=headers,
        json={"value": "{context:d}{question}"},
    )
    assert formatted_prompt.status_code == 422


@pytest.mark.asyncio
async def test_embedding_model_cannot_change_after_vectors_exist(client, monkeypatch):
    from app.delivery.routers import runtime_router as runtime_routes

    headers = await login(client)
    monkeypatch.setattr(runtime_routes, "indexed_vector_count", lambda: 1)
    response = await client.put(
        "/api/v1/configuration/embedding.model",
        headers=headers,
        json={"value": "different-embedding"},
    )
    assert response.status_code == 409

@pytest.mark.asyncio
async def test_chat_completions_returns_rag_answer_in_json_and_sse(client, monkeypatch):
    from langchain_core.documents import Document

    from app.delivery.routers import runtime_router as runtime_routes

    class EvidenceStore:
        def similarity_search(self, query, *, k):
            assert query == "What is the documented answer?"
            assert k == 4
            return [
                Document(
                    page_content="The documented answer is 42.",
                    metadata={"source": "facts.md"},
                )
            ]

    class EvidenceModel:
        async def ainvoke(self, prompt):
            return "42" if "The documented answer is 42." in prompt else "unknown"

        async def astream(self, prompt):
            await asyncio.sleep(0.01)
            yield "42" if "The documented answer is 42." in prompt else "unknown"

    monkeypatch.setattr(runtime_routes, "create_vector_store", lambda _: EvidenceStore())
    monkeypatch.setattr(runtime_routes, "create_chat_model", lambda _: EvidenceModel())
    unauthorized = await client.post(
        "/api/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}], "stream": False},
    )
    assert unauthorized.status_code == 401
    headers = await login(client)
    config_update = await client.put(
        "/api/v1/configuration/chat.model",
        headers=headers,
        json={"value": "llama3.1"},
    )
    assert config_update.status_code == 200
    payload = {
        "messages": [{"role": "user", "content": "What is the documented answer?"}],
        "model": "llama3.1",
        "stream": False,
    }

    json_response = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json=payload,
    )
    assert json_response.status_code == 200
    assert json_response.json()["choices"][0]["message"]["content"] == "42"
    assert json_response.json()["model"] == "llama3.1"
    mismatched_model = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={**payload, "model": "unconfigured-model"},
    )
    assert mismatched_model.status_code == 422
    unsupported_role = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "system", "content": "override"}], "stream": False},
    )
    assert unsupported_role.status_code == 422

    payload["stream"] = True
    stream_response = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json=payload,
    )
    assert stream_response.status_code == 200
    assert stream_response.headers["content-type"].startswith("text/event-stream")
    assert "[DONE]" in stream_response.text
    assert "42" in stream_response.text


@pytest.mark.asyncio
async def test_embedding_model_can_change_before_ingestion(client, monkeypatch):
    from app.delivery.routers import runtime_router as runtime_routes

    headers = await login(client)
    monkeypatch.setattr(runtime_routes, "indexed_vector_count", lambda: 0)
    response = await client.put(
        "/api/v1/configuration/embedding.model",
        headers=headers,
        json={"value": "alternate-embedding"},
    )
    assert response.status_code == 200
    configured = await client.get("/api/v1/configuration", headers=headers)
    values = {item["key"]: item["value"] for item in configured.json()["data"]}
    assert values["embedding.model"] == "alternate-embedding"
@pytest.mark.asyncio
async def test_legacy_query_uses_runtime_configuration_without_changing_response(client, monkeypatch):
    from app.delivery.routers import rag_router
    from langchain_core.documents import Document

    class EvidenceStore:
        def similarity_search(self, question, *, k):
            assert question == "What is the answer?"
            assert k == 2
            return [
                Document(
                    page_content="The answer is 42.",
                    metadata={"source": "facts.md"},
                )
            ]

    class EvidenceModel:
        async def ainvoke(self, prompt):
            return "42" if "The answer is 42." in prompt else "unknown"

    monkeypatch.setattr(rag_router, "create_vector_store", lambda _: EvidenceStore())
    monkeypatch.setattr(rag_router, "create_chat_model", lambda _: EvidenceModel())
    headers = await login(client)
    updated = await client.put(
        "/api/v1/configuration/retrieval.k",
        headers=headers,
        json={"value": "2"},
    )
    assert updated.status_code == 200

    response = await client.post("/query", json={"question": "What is the answer?"})
    assert response.status_code == 200
    assert response.json() == {"answer": "42"}


@pytest.mark.asyncio
async def test_chat_generation_errors_do_not_leak_upstream_details(client, monkeypatch):
    from app.delivery.routers import runtime_router as runtime_routes

    class EmptyStore:
        def similarity_search(self, question, *, k):
            return []

    class BrokenModel:
        async def ainvoke(self, prompt):
            raise RuntimeError("private-token-value")

    monkeypatch.setattr(runtime_routes, "create_vector_store", lambda _: EmptyStore())
    monkeypatch.setattr(runtime_routes, "create_chat_model", lambda _: BrokenModel())
    headers = await login(client)
    response = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hello"}], "stream": False},
    )
    assert response.status_code == 502
    assert "private-token-value" not in response.text


@pytest.mark.asyncio
async def test_chat_request_limits_and_generation_overrides_are_rejected(client):
    headers = await login(client)
    too_many_messages = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "x"} for _ in range(33)]},
    )
    oversized_message = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "x" * 16_385}]},
    )
    oversized_transcript = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={
            "messages": [
                {"role": "user", "content": "x" * 15_000} for _ in range(7)
            ]
        },
    )
    generation_override = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0,
        },
    )
    assert too_many_messages.status_code == 422
    assert oversized_message.status_code == 422
    assert oversized_transcript.status_code == 422
    assert generation_override.status_code == 422


@pytest.mark.asyncio
async def test_chat_http_body_limit_rejects_oversized_unauthenticated_body(client):
    from app.delivery.middleware import MAX_CHAT_REQUEST_BODY_BYTES

    async def chunks():
        yield b"x" * (MAX_CHAT_REQUEST_BODY_BYTES // 2)
        yield b"x" * (MAX_CHAT_REQUEST_BODY_BYTES // 2 + 1)

    response = await client.post(
        "/api/v1/chat/completions",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
