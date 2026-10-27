"""Project-scoped Chroma generations and race-resistant source scanning."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

import chromadb
from filelock import FileLock, Timeout
from langchain_community.embeddings import OllamaEmbeddings
from pathspec import PathSpec

from app.application.rag.config_service import DEFAULT_CONFIGURATION
from app.infrastructure.rag.vector_store import CHROMA_DIRECTORY

PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LEGACY_PROJECT_ID = "legacy"
DEFAULT_INDEX_ROOT = Path("./data/codebase_index")
DEFAULT_MAX_FILE_BYTES = 1_048_576
MAX_GITIGNORE_BYTES = 1_048_576
MAX_GITIGNORE_RULES = 10_000
CHUNK_LINES = 80
CHUNK_OVERLAP = 10
IGNORED_DIRS = {".git", ".hg", ".svn", ".idea", ".venv", "venv", "__pycache__",
                "node_modules", "vendor", "dist", "build", "target", ".tox"}
SECRET_NAMES = {".env", "credentials", "secrets", "id_rsa", "id_dsa", "id_ed25519"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".secret", ".crt", ".der")
TEXT_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs",
                 ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb", ".swift", ".scala",
                 ".sql", ".sh", ".bash", ".zsh", ".fish", ".lua", ".dart", ".ex", ".exs",
                 ".erl", ".hrl", ".fs", ".fsx", ".m", ".mm", ".html", ".css", ".scss",
                 ".vue", ".svelte", ".json", ".yaml", ".yml", ".toml", ".ini", ".xml",
                 ".md", ".rst", ".txt", ".graphql", ".proto", ".dockerfile"}


class CodebaseIndexError(Exception):
    pass


class ProjectBusy(CodebaseIndexError):
    pass


class InvalidProject(CodebaseIndexError):
    pass


class RootNotAllowed(CodebaseIndexError):
    pass


class ScanError(CodebaseIndexError):
    pass


class CodebaseIndexStore:
    def __init__(self, *, index_root: Path | None = None, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES):
        self.root = Path(os.getenv("AURORA_CODEBASE_INDEX_ROOT", str(index_root or DEFAULT_INDEX_ROOT))).resolve()
        self.chroma_root = Path(CHROMA_DIRECTORY).resolve()
        self.max_file_bytes = max_file_bytes

    @staticmethod
    def validate_project_id(project_id: str) -> str:
        if project_id == LEGACY_PROJECT_ID or not PROJECT_ID_RE.fullmatch(project_id):
            raise InvalidProject("invalid project id")
        return project_id

    def _project_dir(self, project_id: str) -> Path:
        self.validate_project_id(project_id)
        return self.root / project_id

    def _lock(self, project_id: str) -> FileLock:
        return FileLock(str(self.root / f".{project_id}.lock"), thread_local=False)

    def _acquire(self, project_id: str) -> FileLock:
        self.validate_project_id(project_id)
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self._lock(project_id)
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise ProjectBusy("project operation already in progress") from exc
        return lock

    def acquire_model_lock(self) -> FileLock:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.root / ".embedding-model.lock"), thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise ProjectBusy("embedding model operation already in progress") from exc
        return lock

    @staticmethod
    def configured_roots() -> list[Path]:
        raw = os.getenv("AURORA_CODEBASE_ROOTS", "")
        return [Path(os.path.abspath(os.path.expanduser(value))) for value in raw.split(os.pathsep) if value.strip()]

    @staticmethod
    def _open_directory_components(path: Path) -> int:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except OSError:
            os.close(fd)
            raise

    def _open_confined_directory(self, requested: str) -> tuple[int, Path]:
        requested_path = Path(os.path.expanduser(requested))
        if not requested_path.is_absolute():
            raise RootNotAllowed("path is not allowed")
        candidate = Path(os.path.abspath(requested_path))
        for root in self.configured_roots():
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            fd = None
            try:
                fd = self._open_directory_components(root)
                for part in relative.parts:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                return fd, relative
            except OSError as exc:
                if fd is not None:
                    os.close(fd)
                if exc.errno == errno.ENOENT:
                    continue
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise RootNotAllowed("path is not allowed") from exc
                raise ScanError("codebase path could not be opened") from exc
        raise RootNotAllowed("path is not allowed")

    @staticmethod
    def _load_gitignore(fd: int, base: str) -> list[tuple[str, PathSpec]]:
        try:
            ignore_fd = os.open(".gitignore", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ScanError("codebase ignore rules could not be read") from exc
        try:
            ignore_stat = os.fstat(ignore_fd)
            if not stat.S_ISREG(ignore_stat.st_mode):
                raise ScanError("codebase ignore rules are not a regular file")
            data = bytearray()
            while len(data) <= MAX_GITIGNORE_BYTES:
                block = os.read(ignore_fd, min(131_072, MAX_GITIGNORE_BYTES + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
            if len(data) > MAX_GITIGNORE_BYTES:
                raise ScanError("codebase ignore rules are too large")
            lines = bytes(data).decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ScanError("codebase ignore rules could not be read") from exc
        finally:
            os.close(ignore_fd)
        if len(lines) > MAX_GITIGNORE_RULES:
            raise ScanError("codebase ignore rules contain too many rules")
        return [(base, PathSpec.from_lines("gitwildmatch", [line.strip()])) for line in lines
                if line.strip() and not line.lstrip().startswith("#")]

    @staticmethod
    def _ignored(relative: str, rules: list[tuple[str, PathSpec]], *, is_dir: bool = False) -> bool:
        ignored = False
        for base, pattern in rules:
            candidate = relative if not base else relative.removeprefix(f"{base}/")
            if is_dir:
                candidate += "/"
            if not base or relative == base or relative.startswith(f"{base}/"):
                for rule in pattern.patterns:
                    match = rule.match_file(candidate)
                    if match is not None:
                        ignored = bool(rule.include)
        return ignored

    @staticmethod
    def _is_secret(name: str) -> bool:
        lower = name.lower()
        return lower in SECRET_NAMES or lower.startswith(".env") or lower.endswith(SECRET_SUFFIXES)

    def _read_source(self, fd: int, relative: str) -> tuple[str | None, bool]:
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ScanError("codebase source is not a regular file")
            if file_stat.st_size > self.max_file_bytes:
                return None, True
            data = bytearray()
            while len(data) <= self.max_file_bytes:
                block = os.read(fd, min(131_072, self.max_file_bytes + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
            if len(data) > self.max_file_bytes:
                return None, True
            if b"\x00" in data:
                return None, False
            return bytes(data).decode("utf-8"), False
        except UnicodeDecodeError:
            return None, False
        except OSError as exc:
            raise ScanError("codebase file could not be read") from exc

    def _scan_directory(self, fd: int, relative: str, rules: list[tuple[str, PathSpec]],
                        files: list[tuple[str, str]], skipped: list[str]) -> None:
        rules = rules + self._load_gitignore(fd, relative)
        try:
            entries = sorted(os.scandir(fd), key=lambda entry: entry.name)
        except OSError as exc:
            raise ScanError("codebase directory could not be read") from exc
        for entry in entries:
            child = f"{relative}/{entry.name}" if relative else entry.name
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in IGNORED_DIRS or self._ignored(child, rules, is_dir=True):
                    continue
                try:
                    child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as exc:
                    raise ScanError("codebase directory could not be opened") from exc
                try:
                    self._scan_directory(child_fd, child, rules, files, skipped)
                finally:
                    os.close(child_fd)
                continue
            if entry.name == ".gitignore" or self._ignored(child, rules):
                continue
            if not entry.is_file(follow_symlinks=False):
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                    special = stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode)
                except OSError as exc:
                    raise ScanError("codebase directory entry could not be inspected") from exc
                if special:
                    raise ScanError("codebase source is not a regular file")
                continue
            if self._is_secret(entry.name):
                continue
            try:
                file_fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            except OSError as exc:
                raise ScanError("codebase file could not be opened") from exc
            try:
                text, was_skipped = self._read_source(file_fd, child)
            finally:
                os.close(file_fd)
            if was_skipped:
                skipped.append(child)
            if text is not None and (Path(entry.name).suffix.lower() in TEXT_SUFFIXES or "." not in entry.name):
                files.append((child, text))

    def _chunks(self, fd: int) -> tuple[list[dict[str, Any]], list[str]]:
        files: list[tuple[str, str]] = []
        skipped: list[str] = []
        try:
            self._scan_directory(fd, "", [], files, skipped)
        finally:
            os.close(fd)
        chunks = []
        for path, text in files:
            lines = text.splitlines()
            for start in range(0, len(lines), CHUNK_LINES - CHUNK_OVERLAP):
                body = lines[start:start + CHUNK_LINES]
                if not body:
                    break
                chunks.append({"content": "\n".join(body), "path": path,
                               "start_line": start + 1, "end_line": start + len(body)})
                if start + CHUNK_LINES >= len(lines):
                    break
        return chunks, skipped

    @staticmethod
    def _embedding_model() -> str:
        return os.getenv("AURORA_CODEBASE_EMBEDDING_MODEL", DEFAULT_CONFIGURATION["embedding.model"])

    @staticmethod
    def _collection_name(project_id: str, generation: str) -> str:
        digest = hashlib.sha256(f"{project_id}:{generation}".encode()).hexdigest()[:48]
        return f"aurora_codebase_{digest}"

    def _client(self):
        self.chroma_root.mkdir(parents=True, exist_ok=True)
        return chromadb.PersistentClient(path=str(self.chroma_root))

    def _metadata_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "current"

    def _cleanup_project_collections(self, project_id: str, *, keep: str | None = None) -> None:
        client = self._client()
        for item in client.list_collections():
            name = item if isinstance(item, str) else item.name
            collection = item if not isinstance(item, str) else client.get_collection(name)
            metadata = collection.metadata or {}
            if metadata.get("project_id") == project_id and name != keep:
                client.delete_collection(name)

    def _published(self, project_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._metadata_path(project_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def index(self, project_id: str, requested_path: str, *, embedding_model: str | None = None,
              model_lock_held: bool = False) -> dict[str, int | str | list[str]]:
        self.validate_project_id(project_id)
        lock = None
        model_lock = None
        collection_name = None
        try:
            if not model_lock_held:
                model_lock = self.acquire_model_lock()
            lock = self._acquire(project_id)
            fd, _ = self._open_confined_directory(requested_path)
            current = self._published(project_id)
            self._cleanup_project_collections(project_id, keep=current.get("collection_name") if current else None)
            chunks, skipped = self._chunks(fd)
            model = embedding_model or self._embedding_model()
            generation = secrets.token_hex(16)
            collection_name = self._collection_name(project_id, generation)
            client = self._client()
            collection = client.create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine", "project_id": project_id,
                          "generation": generation, "embedding_model": model},
            )
            if chunks:
                vectors = OllamaEmbeddings(model=model).embed_documents([item["content"] for item in chunks])
                collection.add(
                    ids=[f"{generation}:{index}" for index in range(len(chunks))],
                    embeddings=vectors,
                    documents=[item["content"] for item in chunks],
                    metadatas=[{key: value for key, value in item.items() if key != "content"} for item in chunks],
                )
            directory = self._project_dir(project_id)
            directory.mkdir(parents=True, exist_ok=True)
            metadata = {"project_id": project_id, "generation": generation,
                        "collection_name": collection_name, "embedding_model": model}
            temporary = directory / f".current-{generation}.tmp"
            temporary.write_text(json.dumps(metadata), encoding="utf-8")
            os.replace(temporary, self._metadata_path(project_id))
            try:
                self._cleanup_project_collections(project_id, keep=collection_name)
            except Exception:
                pass
            return {"project_id": project_id, "indexed_chunks": len(chunks),
                    "skipped_files": len(skipped), "skipped_paths": skipped}
        except Exception:
            if collection_name is not None:
                try:
                    self._client().delete_collection(collection_name)
                except Exception:
                    pass
            raise
        finally:
            if model_lock is not None:
                model_lock.release()
            if lock is not None:
                lock.release()

    def delete(self, project_id: str) -> None:
        self.validate_project_id(project_id)
        lock = self._acquire(project_id)
        try:
            self._cleanup_project_collections(project_id)
            import shutil
            shutil.rmtree(self._project_dir(project_id), ignore_errors=True)
        finally:
            lock.release()

    def projects(self) -> list[str]:
        if not self.root.exists():
            return [LEGACY_PROJECT_ID]
        return [LEGACY_PROJECT_ID] + sorted(item.name for item in self.root.iterdir()
                                            if item.is_dir() and PROJECT_ID_RE.fullmatch(item.name)
                                            and item.name != LEGACY_PROJECT_ID)

    def search(self, project_id: str, query: str, limit: int, *, embedding_model: str | None = None) -> list[dict[str, Any]]:
        if project_id == LEGACY_PROJECT_ID:
            from langchain_chroma import Chroma
            legacy = Chroma(collection_name="source_code",
                            embedding_function=OllamaEmbeddings(model=embedding_model or self._embedding_model()),
                            persist_directory="./data/chroma_data")
            return [{"content": document.page_content, "path": Path(str(document.metadata.get("source", ""))).name,
                     "start_line": None, "end_line": None, "score": float(score)}
                    for document, score in legacy.similarity_search_with_relevance_scores(query, k=limit)]
        self.validate_project_id(project_id)
        self.root.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.root / f".{project_id}.lock"), thread_local=False)
        try:
            try:
                lock.acquire(timeout=30)
            except Timeout as exc:
                raise ProjectBusy("project operation already in progress") from exc
            payload = self._published(project_id)
            if not payload:
                return []
            vector = OllamaEmbeddings(model=payload["embedding_model"]).embed_query(query)
            result = self._client().get_collection(payload["collection_name"]).query(
                query_embeddings=[vector], n_results=limit, include=["documents", "metadatas", "distances"]
            )
            documents = result.get("documents", [[]])[0]
            metadatas = result.get("metadatas", [[]])[0]
            distances = result.get("distances", [[]])[0]
            return [{"content": content, "path": metadata.get("path", ""),
                     "start_line": metadata.get("start_line"), "end_line": metadata.get("end_line"),
                     "score": float(1 - distance)}
                    for content, metadata, distance in zip(documents, metadatas, distances)]
        finally:
            if lock.is_locked:
                lock.release()

    def has_published_chunks(self) -> bool:
        client = self._client()
        for project in self.projects():
            if project == LEGACY_PROJECT_ID:
                continue
            payload = self._published(project)
            if payload and client.get_collection(payload["collection_name"]).count() > 0:
                return True
        return False


def published_chunk_count() -> int:
    store = CodebaseIndexStore()
    client = store._client()
    total = 0
    for project in store.projects():
        if project == LEGACY_PROJECT_ID:
            continue
        payload = store._published(project)
        if payload:
            total += client.get_collection(payload["collection_name"]).count()
    return total
