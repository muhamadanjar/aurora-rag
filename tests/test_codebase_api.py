import asyncio
import os
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
    monkeypatch.setattr(index_store, "CHROMA_DIRECTORY", str(tmp_path / "chroma"))
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
    (root / "Secrets").mkdir()
    (root / "Secrets" / "password.json").write_text("sensitive password", encoding="utf-8")
    (root / "credentials.json").write_text("sensitive credentials", encoding="utf-8")
    (root / "id_ecdsa").write_text("PRIVATE KEY", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00binary")
    (root / "link.py").symlink_to(root / "src" / "main.py")
    headers = {"Authorization": "Bearer codebase-test-token"}

    response = await codebase_client.post("/codebase/projects/demo/index", headers=headers, json={"path": str(root)})
    assert response.status_code == 200
    assert response.json()["indexed_chunks"] == 2
    results = await codebase_client.post("/codebase/search", headers=headers,
                                         json={"project_id": "demo", "query": "alpha", "limit": 5})
    assert results.status_code == 200
    item = next(item for item in results.json()["results"] if item["path"] == "src/main.py")
    assert item["start_line"] == 1 and item["end_line"] == 3
    assert "hidden.py" not in str(results.json()) and "TOKEN" not in str(results.json())
    chroma = index_store.chromadb.PersistentClient(path=str(codebase_env / "chroma"))
    names = [item if isinstance(item, str) else item.name for item in chroma.list_collections()]
    documents = chroma.get_collection(names[0]).get(include=["documents"])["documents"]
    assert any("kept" in document for document in documents)
    assert all("dropped" not in document and "should not be indexed" not in document for document in documents)


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
    chroma = index_store.chromadb.PersistentClient(path=str(codebase_env / "chroma"))
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


@pytest.mark.asyncio
async def test_search_reads_old_generation_during_paused_reindex_then_new_generation(codebase_client, codebase_env, monkeypatch):
    root = codebase_env / "roots" / "snapshot"
    root.mkdir()
    source = root / "main.py"
    source.write_text("old generation", encoding="utf-8")
    headers = {"Authorization": "Bearer codebase-test-token"}
    initial = await codebase_client.post("/codebase/projects/snapshot/index", headers=headers,
                                         json={"path": str(root)})
    assert initial.status_code == 200

    source.write_text("new generation", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    original = index_store.OllamaEmbeddings.embed_documents

    def pause_first_embedding(self, texts):
        if not started.is_set():
            started.set()
            release.wait(timeout=2)
        return original(self, texts)

    monkeypatch.setattr(index_store.OllamaEmbeddings, "embed_documents", pause_first_embedding)
    reindex = asyncio.create_task(codebase_client.post("/codebase/projects/snapshot/index", headers=headers,
                                                       json={"path": str(root)}))
    assert await asyncio.to_thread(started.wait, 1)

    old_search = await codebase_client.post("/codebase/search", headers=headers,
                                            json={"project_id": "snapshot", "query": "old generation", "limit": 5})
    assert old_search.status_code == 200
    assert any("old generation" in item["content"] for item in old_search.json()["results"])

    release.set()
    published = await reindex
    assert published.status_code == 200
    new_search = await codebase_client.post("/codebase/search", headers=headers,
                                            json={"project_id": "snapshot", "query": "new generation", "limit": 5})
    assert new_search.status_code == 200
    assert any("new generation" in item["content"] for item in new_search.json()["results"])


@pytest.mark.asyncio
async def test_search_cleanup_and_publish_are_serialized(codebase_client, codebase_env, monkeypatch):
    root = codebase_env / "roots" / "publication-race"
    root.mkdir()
    source = root / "main.py"
    source.write_text("old snapshot", encoding="utf-8")
    headers = {"Authorization": "Bearer codebase-test-token"}
    initial = await codebase_client.post("/codebase/projects/publication-race/index", headers=headers,
                                         json={"path": str(root)})
    assert initial.status_code == 200
    source.write_text("new snapshot", encoding="utf-8")

    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    original_cleanup = index_store.CodebaseIndexStore._cleanup_project_collections

    def pause_search_cleanup(self, project_id, *, keep=None, include_unpublished=False):
        if not include_unpublished and not cleanup_entered.is_set():
            cleanup_entered.set()
            release_cleanup.wait(timeout=2)
        return original_cleanup(self, project_id, keep=keep, include_unpublished=include_unpublished)

    monkeypatch.setattr(index_store.CodebaseIndexStore, "_cleanup_project_collections", pause_search_cleanup)
    search = asyncio.create_task(codebase_client.post("/codebase/search", headers=headers,
                                                      json={"project_id": "publication-race", "query": "old", "limit": 5}))
    assert await asyncio.to_thread(cleanup_entered.wait, 1)
    reindex = asyncio.create_task(codebase_client.post("/codebase/projects/publication-race/index", headers=headers,
                                                       json={"path": str(root)}))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(reindex), timeout=0.05)
    release_cleanup.set()
    assert (await search).status_code == 200
    assert (await reindex).status_code == 200
    current = await codebase_client.post("/codebase/search", headers=headers,
                                         json={"project_id": "publication-race", "query": "new", "limit": 5})
    assert any("new snapshot" in item["content"] for item in current.json()["results"])


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
async def test_direct_hard_excluded_and_ignored_ancestor_paths_are_rejected(codebase_client, codebase_env, monkeypatch):
    headers = {"Authorization": "Bearer codebase-test-token"}
    monkeypatch.setattr(index_store, "IGNORED_DIRS", set(index_store.IGNORED_DIRS) | {"secrets"})
    secrets = codebase_env / "roots" / "repo" / "Secrets"
    secrets.mkdir(parents=True)
    direct = await codebase_client.post("/codebase/projects/direct-secrets/index", headers=headers,
                                        json={"path": str(secrets)})
    assert direct.status_code == 422

    ignored_root = codebase_env / "roots" / "ignored-ancestor"
    ignored = ignored_root / "private"
    ignored.mkdir(parents=True)
    (ignored_root / ".gitignore").write_text("private/\n", encoding="utf-8")
    ancestor = await codebase_client.post("/codebase/projects/ignored-ancestor/index", headers=headers,
                                          json={"path": str(ignored)})
    assert ancestor.status_code == 422


@pytest.mark.asyncio
async def test_ignore_file_limits_and_special_files_fail_closed(codebase_client, codebase_env):
    from app.infrastructure.codebase.index_store import MAX_GITIGNORE_BYTES, MAX_GITIGNORE_RULES

    headers = {"Authorization": "Bearer codebase-test-token"}
    oversized = codebase_env / "roots" / "oversized"
    oversized.mkdir()
    (oversized / ".gitignore").write_text("x\n" * (MAX_GITIGNORE_RULES + 1), encoding="utf-8")
    too_many_rules = await codebase_client.post("/codebase/projects/oversized/index", headers=headers,
                                                json={"path": str(oversized)})
    assert too_many_rules.status_code == 422

    too_large = codebase_env / "roots" / "too-large"
    too_large.mkdir()
    (too_large / ".gitignore").write_bytes(b"x" * (MAX_GITIGNORE_BYTES + 1))
    too_large_response = await codebase_client.post("/codebase/projects/too-large/index", headers=headers,
                                                    json={"path": str(too_large)})
    assert too_large_response.status_code == 422

    special = codebase_env / "roots" / "special"
    special.mkdir()
    if hasattr(os, "mkfifo"):
        os.mkfifo(special / "input.py")
        fifo_response = await codebase_client.post("/codebase/projects/special/index", headers=headers,
                                                   json={"path": str(special)})
        assert fifo_response.status_code == 422


@pytest.mark.asyncio
async def test_aggregate_limits_fail_and_preserve_previous_generation(codebase_client, codebase_env, monkeypatch):
    root = codebase_env / "roots" / "limits"
    root.mkdir()
    source = root / "main.py"
    source.write_text("previous", encoding="utf-8")
    headers = {"Authorization": "Bearer codebase-test-token"}
    assert (await codebase_client.post("/codebase/projects/limits/index", headers=headers,
                                       json={"path": str(root)})).status_code == 200

    monkeypatch.setattr(index_store, "MAX_REPOSITORY_BYTES", 1)
    source.write_text("replacement", encoding="utf-8")
    limited = await codebase_client.post("/codebase/projects/limits/index", headers=headers,
                                         json={"path": str(root)})
    assert limited.status_code == 422
    preserved = await codebase_client.post("/codebase/search", headers=headers,
                                           json={"project_id": "limits", "query": "previous", "limit": 5})
    assert any("previous" in item["content"] for item in preserved.json()["results"])

    monkeypatch.setattr(index_store, "MAX_REPOSITORY_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(index_store, "MAX_REPOSITORY_FILES", 0)
    file_limited = await codebase_client.post("/codebase/projects/limits/index", headers=headers,
                                              json={"path": str(root)})
    assert file_limited.status_code == 422
    monkeypatch.setattr(index_store, "MAX_REPOSITORY_FILES", 10_000)
    monkeypatch.setattr(index_store, "MAX_REPOSITORY_CHUNKS", 0)
    chunk_limited = await codebase_client.post("/codebase/projects/limits/index", headers=headers,
                                               json={"path": str(root)})
    assert chunk_limited.status_code == 422


@pytest.mark.asyncio
async def test_embedding_and_chroma_batches_are_bounded(codebase_client, codebase_env, monkeypatch):
    root = codebase_env / "roots" / "batching"
    root.mkdir()
    for number in range(130):
        (root / f"file-{number}.py").write_text(f"file {number}", encoding="utf-8")
    batches = []
    original = index_store.OllamaEmbeddings.embed_documents

    def record_batch(self, texts):
        batches.append(len(texts))
        return original(self, texts)

    monkeypatch.setattr(index_store.OllamaEmbeddings, "embed_documents", record_batch)
    response = await codebase_client.post("/codebase/projects/batching/index",
                                         headers={"Authorization": "Bearer codebase-test-token"},
                                         json={"path": str(root)})
    assert response.status_code == 200
    assert batches == [128, 2]


@pytest.mark.asyncio
async def test_scan_pattern_deadline_and_enumeration_budgets_fail_closed(codebase_client, codebase_env, monkeypatch):
    headers = {"Authorization": "Bearer codebase-test-token"}

    unsafe = codebase_env / "roots" / "unsafe-pattern"
    unsafe.mkdir()
    (unsafe / ".gitignore").write_text("*a*b*\n", encoding="utf-8")
    response = await codebase_client.post("/codebase/projects/unsafe-pattern/index", headers=headers,
                                          json={"path": str(unsafe)})
    assert response.status_code == 422

    aggregate_rules = codebase_env / "roots" / "aggregate-rules"
    nested = aggregate_rules / "nested"
    nested.mkdir(parents=True)
    (aggregate_rules / ".gitignore").write_text("one.py\n", encoding="utf-8")
    (nested / ".gitignore").write_text("two.py\n", encoding="utf-8")
    monkeypatch.setattr(index_store, "MAX_TOTAL_GITIGNORE_RULES", 1)
    response = await codebase_client.post("/codebase/projects/aggregate-rules/index", headers=headers,
                                          json={"path": str(aggregate_rules)})
    assert response.status_code == 422
    monkeypatch.setattr(index_store, "MAX_TOTAL_GITIGNORE_RULES", 50_000)
    monkeypatch.setattr(index_store, "MAX_TOTAL_GITIGNORE_BYTES", 1)
    response = await codebase_client.post("/codebase/projects/aggregate-rules-bytes/index", headers=headers,
                                          json={"path": str(aggregate_rules)})
    assert response.status_code == 422
    monkeypatch.setattr(index_store, "MAX_TOTAL_GITIGNORE_BYTES", 8 * 1024 * 1024)

    visited = codebase_env / "roots" / "visited-budget"
    visited.mkdir()
    (visited / "one.py").write_text("one", encoding="utf-8")
    (visited / "two.py").write_text("two", encoding="utf-8")
    monkeypatch.setattr(index_store, "MAX_VISITED_ENTRIES", 1)
    response = await codebase_client.post("/codebase/projects/visited-budget/index", headers=headers,
                                          json={"path": str(visited)})
    assert response.status_code == 422

    depth = codebase_env / "roots" / "depth-budget" / "one" / "two"
    depth.mkdir(parents=True)
    (depth / "main.py").write_text("deep", encoding="utf-8")
    monkeypatch.setattr(index_store, "MAX_VISITED_ENTRIES", 100_000)
    monkeypatch.setattr(index_store, "MAX_DIRECTORY_DEPTH", 1)
    response = await codebase_client.post("/codebase/projects/depth-budget/index", headers=headers,
                                          json={"path": str(codebase_env / "roots" / "depth-budget")})
    assert response.status_code == 422

    deadline = codebase_env / "roots" / "deadline-budget"
    deadline.mkdir()
    (deadline / "main.py").write_text("deadline", encoding="utf-8")
    monkeypatch.setattr(index_store, "MAX_DIRECTORY_DEPTH", 64)
    monkeypatch.setattr(index_store, "MAX_SCAN_SECONDS", 0)
    response = await codebase_client.post("/codebase/projects/deadline-budget/index", headers=headers,
                                          json={"path": str(deadline)})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_gitignore_regex_timeout_fails_as_scan_error(codebase_client, codebase_env, monkeypatch):
    root = codebase_env / "roots" / "regex-timeout"
    root.mkdir()
    (root / ".gitignore").write_text("**/*.py\n", encoding="utf-8")
    (root / "main.py").write_text("timeout", encoding="utf-8")

    class TimedOutPattern:
        def match(self, value, *, timeout):
            raise TimeoutError

    monkeypatch.setattr(index_store.safe_regex, "compile", lambda *args: TimedOutPattern())
    response = await codebase_client.post("/codebase/projects/regex-timeout/index",
                                          headers={"Authorization": "Bearer codebase-test-token"},
                                          json={"path": str(root)})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_model_lock_can_be_released_by_event_loop_after_thread_acquire(codebase_env):
    store = CodebaseIndexStore()
    first = await asyncio.to_thread(store.acquire_model_lock)
    first.release()
    second = await asyncio.to_thread(store.acquire_model_lock)
    second.release()


@pytest.mark.asyncio
async def test_directory_only_ignore_skips_unreadable_special_children(codebase_client, codebase_env):
    root = codebase_env / "roots" / "ignored-directory"
    ignored = root / "ignored"
    ignored.mkdir(parents=True)
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "main.py").write_text("indexed", encoding="utf-8")
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO test requires mkfifo")
    os.mkfifo(ignored / "blocked.py")
    response = await codebase_client.post(
        "/codebase/projects/ignored-directory/index",
        headers={"Authorization": "Bearer codebase-test-token"},
        json={"path": str(root)},
    )
    assert response.status_code == 200
    assert response.json()["indexed_chunks"] == 1



@pytest.mark.asyncio
async def test_search_project_busy_is_http_409(codebase_client, monkeypatch):
    from app.delivery.routers import codebase_router

    def busy(*args, **kwargs):
        raise ProjectBusy("project operation already in progress")

    monkeypatch.setattr(codebase_router.CodebaseService, "search", busy)
    response = await codebase_client.post(
        "/codebase/search",
        headers={"Authorization": "Bearer codebase-test-token"},
        json={"project_id": "demo", "query": "query", "limit": 1},
    )
    assert response.status_code == 409
