"""Project-scoped Chroma generations and race-resistant source scanning."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
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
MAX_REPOSITORY_BYTES = 64 * 1024 * 1024
MAX_REPOSITORY_FILES = 10_000
MAX_REPOSITORY_CHUNKS = 50_000
EMBEDDING_BATCH_SIZE = 128
MAX_SCAN_SECONDS = 60
MAX_VISITED_ENTRIES = 100_000
MAX_DIRECTORY_DEPTH = 64
MAX_GITIGNORE_BYTES = 1_048_576
MAX_GITIGNORE_RULES = 10_000
MAX_TOTAL_GITIGNORE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_GITIGNORE_RULES = 50_000
MAX_GITIGNORE_PATTERN_BYTES = 4096
MAX_GITIGNORE_WILDCARDS = 64
CHUNK_LINES = 80
CHUNK_OVERLAP = 10
IGNORED_DIRS = {".git", ".hg", ".svn", ".idea", ".venv", "venv", "__pycache__",
                "node_modules", "vendor", "dist", "build", "target", ".tox",
                ".ssh", ".aws", "secrets", "credentials"}
SECRET_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json",
                "secrets", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".secret", ".crt", ".der", ".tfstate")
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

    @staticmethod
    def _check_scan_deadline(state: dict[str, Any]) -> None:
        if time.monotonic() >= state["deadline"]:
            raise ScanError("codebase scan exceeded its deadline")

    @staticmethod
    def _validate_gitignore_pattern(line: str) -> None:
        if len(line.encode("utf-8")) > MAX_GITIGNORE_PATTERN_BYTES:
            raise ScanError("codebase ignore pattern is too large")
        if line.count("*") + line.count("?") > MAX_GITIGNORE_WILDCARDS:
            raise ScanError("codebase ignore pattern is too complex")
        for segment in line.split("/"):
            runs = re.findall(r"[*?]+", segment)
            if len(runs) > 1:
                raise ScanError("codebase ignore pattern is too complex")
        bracket_depth = 0
        for character in line:
            if character == "[":
                bracket_depth += 1
            elif character == "]":
                bracket_depth -= 1
                if bracket_depth < 0:
                    raise ScanError("codebase ignore pattern is invalid")
        if bracket_depth:
            raise ScanError("codebase ignore pattern is invalid")

    def _open_confined_directory(self, requested: str, state: dict[str, Any]) -> tuple[int, Path, list[tuple[str, PathSpec]]]:
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
                self._check_scan_deadline(state)
                if len(relative.parts) > MAX_DIRECTORY_DEPTH:
                    raise ScanError("codebase directory depth is too large")
                fd = self._open_directory_components(root)
                rules = self._load_gitignore(fd, "", state)
                traversed = ""
                for part in relative.parts:
                    child = f"{traversed}/{part}" if traversed else part
                    if self._is_hard_excluded_directory(part) or self._ignored(child, rules, is_dir=True, state=state):
                        os.close(fd)
                        fd = None
                        raise RootNotAllowed("path is not allowed")
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                    traversed = child
                    rules = rules + self._load_gitignore(fd, traversed, state)
                return fd, relative, rules
            except CodebaseIndexError:
                if fd is not None:
                    os.close(fd)
                raise
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
    def _load_gitignore(fd: int, base: str, state: dict[str, Any]) -> list[tuple[str, PathSpec]]:
        CodebaseIndexStore._check_scan_deadline(state)
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
                CodebaseIndexStore._check_scan_deadline(state)
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
        state["gitignore_bytes"] += len(data)
        state["gitignore_rules"] += sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in lines)
        if len(lines) > MAX_GITIGNORE_RULES or state["gitignore_rules"] > MAX_TOTAL_GITIGNORE_RULES:
            raise ScanError("codebase ignore rules contain too many rules")
        if state["gitignore_bytes"] > MAX_TOTAL_GITIGNORE_BYTES:
            raise ScanError("codebase ignore rules contain too many bytes")
        patterns = []
        for line in lines:
            CodebaseIndexStore._check_scan_deadline(state)
            stripped = line.strip()
            if not stripped or line.lstrip().startswith("#"):
                continue
            CodebaseIndexStore._validate_gitignore_pattern(stripped)
            try:
                patterns.append((base, PathSpec.from_lines("gitwildmatch", [stripped])))
            except Exception as exc:
                raise ScanError("codebase ignore pattern is invalid") from exc
            CodebaseIndexStore._check_scan_deadline(state)
        return patterns

    @staticmethod
    def _ignored(relative: str, rules: list[tuple[str, PathSpec]], *, is_dir: bool = False,
                 state: dict[str, Any] | None = None) -> bool:
        if len(relative.encode("utf-8")) > MAX_GITIGNORE_PATTERN_BYTES:
            raise ScanError("codebase path is too long for ignore matching")
        ignored = False
        for base, pattern in rules:
            if state is not None:
                CodebaseIndexStore._check_scan_deadline(state)
            candidate = relative if not base else relative.removeprefix(f"{base}/")
            if is_dir:
                candidate += "/"
            if not base or relative == base or relative.startswith(f"{base}/"):
                for rule in pattern.patterns:
                    try:
                        match = rule.match_file(candidate)
                    except TimeoutError as exc:
                        raise ScanError("codebase ignore matching exceeded its deadline") from exc
                    except Exception as exc:
                        raise ScanError("codebase ignore matching failed") from exc
                    if match is not None:
                        ignored = bool(rule.include)
                    if state is not None:
                        CodebaseIndexStore._check_scan_deadline(state)
        return ignored

    @staticmethod
    def _is_secret(name: str) -> bool:
        lower = name.lower()
        return lower in SECRET_NAMES or lower.startswith(".env") or lower.endswith(SECRET_SUFFIXES)

    @staticmethod
    def _is_hard_excluded_directory(name: str) -> bool:
        return name.casefold() in {value.casefold() for value in IGNORED_DIRS}

    def _read_source(self, fd: int, relative: str, state: dict[str, Any]) -> tuple[str | None, bool, int]:
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ScanError("codebase source is not a regular file")
            if file_stat.st_size > self.max_file_bytes:
                return None, True, file_stat.st_size
            data = bytearray()
            while len(data) <= self.max_file_bytes:
                self._check_scan_deadline(state)
                block = os.read(fd, min(131_072, self.max_file_bytes + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
            if len(data) > self.max_file_bytes:
                return None, True, file_stat.st_size
            if b"\x00" in data:
                return None, False, file_stat.st_size
            return bytes(data).decode("utf-8"), False, file_stat.st_size
        except UnicodeDecodeError:
            return None, False, file_stat.st_size
        except OSError as exc:
            raise ScanError("codebase file could not be read") from exc

    def _scan_directory(self, fd: int, relative: str, rules: list[tuple[str, PathSpec]],
                        files: list[tuple[str, str]], skipped: list[str], stats: dict[str, int],
                        display_relative: str = "", depth: int = 0) -> None:
        self._check_scan_deadline(stats)
        if depth > MAX_DIRECTORY_DEPTH:
            raise ScanError("codebase directory depth is too large")
        entries = []
        try:
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    self._check_scan_deadline(stats)
                    stats["visited"] += 1
                    if stats["visited"] > MAX_VISITED_ENTRIES:
                        raise ScanError("codebase contains too many directory entries")
                    entries.append(entry)
        except ScanError:
            raise
        except OSError as exc:
            raise ScanError("codebase directory could not be read") from exc
        self._check_scan_deadline(stats)
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            self._check_scan_deadline(stats)
            child = f"{relative}/{entry.name}" if relative else entry.name
            display_child = f"{display_relative}/{entry.name}" if display_relative else entry.name
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if self._is_hard_excluded_directory(entry.name) or self._ignored(child, rules, is_dir=True, state=stats):
                    continue
                try:
                    child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as exc:
                    raise ScanError("codebase directory could not be opened") from exc
                try:
                    child_rules = rules + self._load_gitignore(child_fd, child, stats)
                    self._scan_directory(child_fd, child, child_rules, files, skipped, stats,
                                         display_child, depth + 1)
                finally:
                    os.close(child_fd)
                continue
            if entry.name == ".gitignore" or self._ignored(child, rules, state=stats):
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
                text, was_skipped, byte_count = self._read_source(file_fd, child, stats)
            finally:
                os.close(file_fd)
            stats["files"] += 1
            stats["bytes"] += byte_count
            if stats["files"] > MAX_REPOSITORY_FILES:
                raise ScanError("repository contains too many files")
            if stats["bytes"] > MAX_REPOSITORY_BYTES:
                raise ScanError("repository contains too many bytes")
            self._check_scan_deadline(stats)
            if was_skipped:
                skipped.append(display_child)
            if text is not None and (Path(entry.name).suffix.lower() in TEXT_SUFFIXES or "." not in entry.name):
                files.append((display_child, text))

    def _chunks(self, fd: int, relative: Path, rules: list[tuple[str, PathSpec]],
                stats: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        files: list[tuple[str, str]] = []
        skipped: list[str] = []
        stats.setdefault("visited", 0)
        stats.setdefault("files", 0)
        stats.setdefault("bytes", 0)
        try:
            rule_relative = relative.as_posix() if relative.as_posix() != "." else ""
            self._scan_directory(fd, rule_relative, rules, files, skipped, stats)
        finally:
            os.close(fd)
        chunks = []
        for path, text in files:
            self._check_scan_deadline(stats)
            lines = text.splitlines()
            for start in range(0, len(lines), CHUNK_LINES - CHUNK_OVERLAP):
                self._check_scan_deadline(stats)
                body = lines[start:start + CHUNK_LINES]
                if not body:
                    break
                chunks.append({"content": "\n".join(body), "path": path,
                               "start_line": start + 1, "end_line": start + len(body)})
                if len(chunks) > MAX_REPOSITORY_CHUNKS:
                    raise ScanError("repository contains too many chunks")
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

    def _generation_path(self, project_id: str, generation: str) -> Path:
        return self._project_dir(project_id) / f"generation-{generation}.json"

    def _building_generation_path(self, project_id: str, generation: str) -> Path:
        return self._project_dir(project_id) / f"generation-{generation}.building"

    def _publication_lock_path(self, project_id: str) -> Path:
        return self.root / f".{project_id}.publication.lock"

    def _generation_lock_path(self, project_id: str, generation: str) -> Path:
        return self.root / f".{project_id}.{generation}.readers.lock"

    @staticmethod
    def _open_flock(path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)

    @staticmethod
    def _release_flock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _cleanup_project_collections(self, project_id: str, *, keep: str | None = None,
                                     include_unpublished: bool = False) -> bool:
        client = self._client()
        candidates = []
        for item in client.list_collections():
            name = item if isinstance(item, str) else item.name
            collection = item if not isinstance(item, str) else client.get_collection(name)
            metadata = collection.metadata or {}
            if metadata.get("project_id") != project_id or name == keep:
                continue
            generation = metadata.get("generation")
            if not include_unpublished and (not generation or not self._generation_path(project_id, generation).exists()):
                continue
            candidates.append((name, generation))
        locks = []
        try:
            for _, generation in candidates:
                if not generation:
                    continue
                reader_fd = self._open_flock(self._generation_lock_path(project_id, generation))
                try:
                    fcntl.flock(reader_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self._release_flock(reader_fd)
                    return False
                locks.append((reader_fd, generation))
            for name, generation in candidates:
                client.delete_collection(name)
                if generation:
                    try:
                        self._generation_path(project_id, generation).unlink()
                    except FileNotFoundError:
                        pass
            return True
        finally:
            for reader_fd, _ in locks:
                self._release_flock(reader_fd)

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
        generation = None
        published = False
        fd = None
        scan_state = {
            "deadline": time.monotonic() + MAX_SCAN_SECONDS,
            "visited": 0,
            "files": 0,
            "bytes": 0,
            "gitignore_bytes": 0,
            "gitignore_rules": 0,
        }
        try:
            if not model_lock_held:
                model_lock = self.acquire_model_lock()
            lock = self._acquire(project_id)
            fd, relative, inherited_rules = self._open_confined_directory(requested_path, scan_state)
            initial_publication_fd = self._open_flock(self._publication_lock_path(project_id))
            try:
                fcntl.flock(initial_publication_fd, fcntl.LOCK_EX)
                current = self._published(project_id)
                self._cleanup_project_collections(
                    project_id,
                    keep=current.get("collection_name") if current else None,
                    include_unpublished=True,
                )
            finally:
                self._release_flock(initial_publication_fd)
            scan_fd = fd
            fd = None
            chunks, skipped = self._chunks(scan_fd, relative, inherited_rules, scan_state)
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
                embeddings = OllamaEmbeddings(model=model)
                for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
                    batch = chunks[start:start + EMBEDDING_BATCH_SIZE]
                    vectors = embeddings.embed_documents([item["content"] for item in batch])
                    collection.add(
                        ids=[f"{generation}:{index}" for index in range(start, start + len(batch))],
                        embeddings=vectors,
                        documents=[item["content"] for item in batch],
                        metadatas=[{key: value for key, value in item.items() if key != "content"} for item in batch],
                    )
            directory = self._project_dir(project_id)
            directory.mkdir(parents=True, exist_ok=True)
            metadata = {"project_id": project_id, "generation": generation,
                        "collection_name": collection_name, "embedding_model": model}
            generation_temp = self._building_generation_path(project_id, generation)
            generation_temp.write_text(json.dumps(metadata), encoding="utf-8")
            temporary = directory / f".current-{generation}.tmp"
            temporary.write_text(json.dumps(metadata), encoding="utf-8")
            publication_fd = self._open_flock(self._publication_lock_path(project_id))
            try:
                fcntl.flock(publication_fd, fcntl.LOCK_EX)
                os.replace(temporary, self._metadata_path(project_id))
                published = True
                try:
                    os.replace(generation_temp, self._generation_path(project_id, generation))
                except OSError:
                    # The pointer is already the publication boundary. A later
                    # operation can reap this unmarked generation safely.
                    pass
                try:
                    self._cleanup_project_collections(
                        project_id, keep=collection_name, include_unpublished=True
                    )
                except Exception:
                    pass
            finally:
                try:
                    self._release_flock(publication_fd)
                except OSError:
                    if not published:
                        raise
            return {"project_id": project_id, "indexed_chunks": len(chunks),
                    "skipped_files": len(skipped), "skipped_paths": skipped}
        except Exception:
            if collection_name is not None and not published:
                try:
                    self._client().delete_collection(collection_name)
                except Exception:
                    pass
            if collection_name is not None and generation is not None and not published:
                try:
                    self._building_generation_path(project_id, generation).unlink()
                except FileNotFoundError:
                    pass
                try:
                    self._generation_path(project_id, generation).unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            if fd is not None:
                os.close(fd)
            if model_lock is not None:
                model_lock.release()
            if lock is not None:
                lock.release()

    def delete(self, project_id: str) -> None:
        self.validate_project_id(project_id)
        lock = self._acquire(project_id)
        publication_fd = None
        try:
            publication_fd = self._open_flock(self._publication_lock_path(project_id))
            fcntl.flock(publication_fd, fcntl.LOCK_EX)
            if not self._cleanup_project_collections(project_id, include_unpublished=True):
                raise ProjectBusy("project has active readers")
            import shutil
            shutil.rmtree(self._project_dir(project_id), ignore_errors=True)
        finally:
            if publication_fd is not None:
                self._release_flock(publication_fd)
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
        publication_fd = self._open_flock(self._publication_lock_path(project_id))
        reader_fd = None
        try:
            fcntl.flock(publication_fd, fcntl.LOCK_SH)
            payload = self._published(project_id)
            if not payload:
                return []
            reader_fd = self._open_flock(
                self._generation_lock_path(project_id, payload["generation"])
            )
            fcntl.flock(reader_fd, fcntl.LOCK_SH)
            self._release_flock(publication_fd)
            publication_fd = None
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
            if publication_fd is not None:
                self._release_flock(publication_fd)
            if reader_fd is not None:
                self._release_flock(reader_fd)
            try:
                cleanup_publication_fd = self._open_flock(self._publication_lock_path(project_id))
                try:
                    fcntl.flock(cleanup_publication_fd, fcntl.LOCK_EX)
                    current = self._published(project_id)
                    self._cleanup_project_collections(
                        project_id, keep=current.get("collection_name") if current else None
                    )
                finally:
                    self._release_flock(cleanup_publication_fd)
            except Exception:
                pass

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
