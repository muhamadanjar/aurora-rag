import asyncio
import threading

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.infrastructure.codebase import index_store
from app.infrastructure.codebase.index_store import CodebaseIndexStore, ProjectBusy
from app.main import app


class FakeEmbeddings:
    def __init__(self, model: str):
        self.model = model

    def embed_documents(self, texts):
        return [[float(len(text)), float(text.lower().count("secret")), 1.0] for text in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


@pytest.fixture
def codebase_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AURORA_CODEBASE_TOKEN", "codebase-test-token")
    monkeypatch.setenv("AURORA_CODEBASE_ROOTS", str(tmp_path / "roots"))
    monkeypatch.setenv("AURORA_CODEBASE_INDEX_ROOT", str(tmp_path / "index"))
    (tmp_path / "roots").mkdir()
    monkeypatch.setattr(index_store, "OllamaEmbeddings", FakeEmbeddings)
    return tmp_path


@pytest_asyncio.fixture
async def codebase_client(codebase_env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_codebase_endpoints_fail_closed_and_require_exact_bearer(codebase_client, monkeypatch):
    monkeypatch.delenv("AURORA_CODEBASE_TOKEN")
    assert (await codebase_client.get("/codebase/projects")).status_code == 503
    monkeypatch.setenv("AURORA_CODEBASE_TOKEN", "codebase-test-token")
    assert (await codebase_client.get("/codebase/projects")).status_code == 401
    assert (await codebase_client.get("/codebase/projects", headers={"Authorization": "Bearer codebase-test-token"})).status_code == 200


@pytest.mark.asyncio
async def test_codebase_body_limit_runs_before_auth_and_json_parsing(codebase_client):
    from app.delivery.middleware import MAX_CODEBASE_REQUEST_BODY_BYTES

    async def chunks():
        payload = b'{"project_id":"demo","query":"' + b"x" * (MAX_CODEBASE_REQUEST_BODY_BYTES + 32) + b'","limit":1}'
        for offset in range(0, len(payload), 4096):
            yield payload[offset:offset + 4096]

    unauthenticated = await codebase_client.post("/codebase/search", content=chunks(),
                                                 headers={"Content-Type": "application/json"})
    assert unauthenticated.status_code == 413
    authenticated = await codebase_client.post(
        "/codebase/projects/demo/index", content=chunks(),
        headers={"Authorization": "Bearer codebase-test-token", "Content-Type": "application/json"},
    )
    assert authenticated.status_code == 413


@pytest.mark.asyncio
async def test_index_scans_safe_text_with_nested_gitignore_and_line_metadata(codebase_client, codebase_env):
    root = codebase_env / "roots" / "repo"
    nested = root / "src" / "ignored"
    nested.mkdir(parents=True)
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "src" / ".gitignore").write_text("drop.py\n!keep.py\n", encoding="utf-8")
    (root / "src" / "main.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (root / "src" / "keep.py").write_text("kept", encoding="utf-8")
    (root / "src" / "drop.py").write_text("dropped", encoding="utf-8")
    (nested / "hidden.py").write_text("should not be indexed", encoding="utf-8")
    (root / ".env").write_text("TOKEN=do-not-index", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00binary")
    (root / "link.py").symlink_to(root / "src" / "main.py")
    headers = {"Authorization": "Bearer codebase-test-token"}

    response = await codebase_client.post("/codebase/projects/demo/index", headers=headers, json={"path": str(root)})
    assert response.status_code == 200
    assert response.json()["indexed_chunks"] == 2
    results = await codebase_client.post("/codebase/search", headers=headers,
                                         json={"project_id": "demo", "query": "alpha", "limit": 5})
    assert results.status_code == 200
    item = results.json()["results"][0]
    assert item["path"] == "src/main.py"
    assert item["start_line"] == 1 and item["end_line"] == 3
    assert "hidden.py" not in str(results.json()) and "TOKEN" not in str(results.json())


@pytest.mark.asyncio
async def test_reindex_removes_stale_chunks_and_failed_build_keeps_published_generation(codebase_client, codebase_env, monkeypatch):
    root = codebase_env / "roots" / "repo"
    root.mkdir()
    source = root / "one.py"
    source.write_text("old marker", encoding="utf-8")
    headers = {"Authorization": "Bearer codebase-test-token"}
    assert (await codebase_client.post("/codebase/projects/demo/index", headers=headers, json={"path": str(root)})).status_code == 200
    source.unlink()
    (root / "two.py").write_text("new marker", encoding="utf-8")
    assert (await codebase_client.post("/codebase/projects/demo/index", headers=headers, json={"path": str(root)})).status_code == 200
    results = await codebase_client.post("/codebase/search", headers=headers,
                                         json={"project_id": "demo", "query": "new", "limit": 5})
    assert all("old marker" not in item["content"] for item in results.json()["results"])

    original_embed = index_store.OllamaEmbeddings.embed_documents
    monkeypatch.setattr(index_store.OllamaEmbeddings, "embed_documents",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("ollama down")))
    failed = await codebase_client.post("/codebase/projects/demo/index", headers=headers, json={"path": str(root)})
    assert failed.status_code == 502
    monkeypatch.setattr(index_store.OllamaEmbeddings, "embed_documents", original_embed)
    preserved = await codebase_client.post("/codebase/search", headers=headers,
                                           json={"project_id": "demo", "query": "new", "limit": 5})
    assert preserved.json()["results"]
    chroma = index_store.chromadb.PersistentClient(path=str(codebase_env / "index" / "chroma"))
    names = [item if isinstance(item, str) else item.name for item in chroma.list_collections()]
    assert len([name for name in names if name.startswith("aurora_codebase_")]) == 1


@pytest.mark.asyncio
async def test_same_project_http_indexes_conflict_without_blocking_event_loop(codebase_client, codebase_env, monkeypatch):
    root = codebase_env / "roots" / "repo"
    root.mkdir()
    (root / "main.py").write_text("content", encoding="utf-8")
    headers = {"Authorization": "Bearer codebase-test-token"}
    started = threading.Event()
    release = threading.Event()
    original = index_store.OllamaEmbeddings.embed_documents

    def blocking_embed(self, texts):
        started.set()
        release.wait(timeout=2)
        return original(self, texts)

    monkeypatch.setattr(index_store.OllamaEmbeddings, "embed_documents", blocking_embed)
    first = asyncio.create_task(codebase_client.post("/codebase/projects/demo/index", headers=headers,
                                                     json={"path": str(root)}))
    assert await asyncio.to_thread(started.wait, 1)
    second = await codebase_client.post("/codebase/projects/demo/index", headers=headers,
                                        json={"path": str(root)})
    release.set()
    first_response = await first
    assert first_response.status_code == 200
    assert second.status_code == 409


def test_same_project_operations_are_rejected_across_workers(codebase_env):
    store = CodebaseIndexStore()
    lock = store._acquire("demo")
    try:
        with pytest.raises(ProjectBusy):
            store._acquire("demo")
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_path_confinement_and_project_listing(codebase_client, codebase_env):
    headers = {"Authorization": "Bearer codebase-test-token"}
    assert (await codebase_client.post("/codebase/projects/legacy/index", headers=headers,
                                       json={"path": str(codebase_env / "roots")})).status_code == 422
    assert (await codebase_client.delete("/codebase/projects/legacy", headers=headers)).status_code == 422
    outside = codebase_env / "outside"
    outside.mkdir()
    (codebase_env / "roots" / "escape").symlink_to(outside, target_is_directory=True)
    symlinked = await codebase_client.post("/codebase/projects/demo/index", headers=headers,
                                           json={"path": str(codebase_env / "roots" / "escape")})
    assert symlinked.status_code == 422
    rejected = await codebase_client.post("/codebase/projects/../bad/index", headers=headers, json={"path": str(outside)})
    assert rejected.status_code in {404, 422}
    projects = await codebase_client.get("/codebase/projects", headers=headers)
    assert projects.json() == {"projects": [{"project_id": "legacy"}]}


@pytest.mark.asyncio
async def test_model_lock_can_be_released_by_event_loop_after_thread_acquire(codebase_env):
    store = CodebaseIndexStore()
    first = await asyncio.to_thread(store.acquire_model_lock)
    first.release()
    second = await asyncio.to_thread(store.acquire_model_lock)
    second.release()
