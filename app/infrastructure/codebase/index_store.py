"""Atomic, filesystem-backed generations for project source indexes.

The store deliberately does not use the legacy Chroma collection. Published
generation files are immutable; the current-generation pointer is replaced in
one filesystem operation only after a complete scan and embedding build.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from langchain_community.embeddings import OllamaEmbeddings

from app.application.rag.config_service import DEFAULT_CONFIGURATION

PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LEGACY_PROJECT_ID = "legacy"
DEFAULT_INDEX_ROOT = Path("./data/codebase_index")
DEFAULT_MAX_FILE_BYTES = 1_048_576
CHUNK_LINES = 80
CHUNK_OVERLAP = 10
IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".venv", "venv", "__pycache__",
    "node_modules", "vendor", "dist", "build", "target", ".tox",
}
SECRET_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_dsa", "id_ed25519", "credentials", "secrets",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".secret", ".crt", ".der")
TEXT_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb", ".swift", ".scala",
    ".sql", ".sh", ".bash", ".zsh", ".fish", ".lua", ".dart", ".ex", ".exs",
    ".erl", ".hrl", ".fs", ".fsx", ".m", ".mm", ".html", ".css", ".scss",
    ".vue", ".svelte", ".json", ".yaml", ".yml", ".toml", ".ini", ".xml",
    ".md", ".rst", ".txt", ".graphql", ".proto", ".dockerfile",
}


class CodebaseIndexError(Exception):
    pass


class ProjectBusy(CodebaseIndexError):
    pass


class InvalidProject(CodebaseIndexError):
    pass


class RootNotAllowed(CodebaseIndexError):
    pass


class CodebaseIndexStore:
    def __init__(self, *, index_root: Path | None = None, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES):
        self.root = Path(os.getenv("AURORA_CODEBASE_INDEX_ROOT", str(index_root or DEFAULT_INDEX_ROOT))).resolve()
        self.max_file_bytes = max_file_bytes

    @staticmethod
    def validate_project_id(project_id: str) -> str:
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise InvalidProject("invalid project id")
        return project_id

    def _project_dir(self, project_id: str) -> Path:
        self.validate_project_id(project_id)
        return self.root / project_id

    def _lock(self, project_id: str) -> FileLock:
        return FileLock(str(self.root / f".{project_id}.lock"))

    def _acquire(self, project_id: str) -> FileLock:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self._lock(project_id)
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise ProjectBusy("project operation already in progress") from exc
        return lock

    @staticmethod
    def configured_roots() -> list[Path]:
        raw = os.getenv("AURORA_CODEBASE_ROOTS", "")
        return [Path(value).expanduser().resolve() for value in raw.split(os.pathsep) if value.strip()]

    def _confined_path(self, requested: str) -> tuple[Path, Path]:
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            raise RootNotAllowed("path is not allowed")
        if any(part.is_symlink() for part in (candidate, *candidate.parents)):
            raise RootNotAllowed("path is not allowed")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RootNotAllowed("path is not allowed") from exc
        for root in self.configured_roots():
            try:
                return root, resolved.relative_to(root)
            except ValueError:
                continue
        raise RootNotAllowed("path is not allowed")

    @staticmethod
    def _gitignore_patterns(directory: Path) -> list[str]:
        try:
            return [line.strip() for line in (directory / ".gitignore").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        except (OSError, UnicodeDecodeError):
            return []

    @staticmethod
    def _ignored(path: Path, base: Path, patterns: list[str]) -> bool:
        relative = path.relative_to(base).as_posix()
        name = path.name
        for pattern in patterns:
            negated = pattern.startswith("!")
            pattern = pattern[1:] if negated else pattern
            if pattern.endswith("/"):
                pattern = pattern[:-1]
            matched = path.match(pattern) or Path(relative).match(pattern) or name == pattern
            if matched and not negated:
                return True
        return False

    def _iter_files(self, base: Path):
        stack = [(base, self._gitignore_patterns(base))]
        while stack:
            directory, inherited = stack.pop()
            try:
                entries = sorted(directory.iterdir(), key=lambda item: item.name)
            except OSError:
                continue
            patterns = inherited + self._gitignore_patterns(directory)
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name in IGNORED_DIRS or self._ignored(entry, base, patterns):
                        continue
                    stack.append((entry, patterns))
                elif entry.is_file() and entry.name != ".gitignore" and not self._ignored(entry, base, patterns):
                    yield entry

    @staticmethod
    def _is_secret(path: Path) -> bool:
        name = path.name.lower()
        return name in SECRET_NAMES or name.startswith(".env") or name.endswith(SECRET_SUFFIXES)

    def _chunks(self, base: Path) -> tuple[list[dict[str, Any]], int]:
        chunks: list[dict[str, Any]] = []
        skipped = 0
        for path in self._iter_files(base):
            if self._is_secret(path):
                continue
            try:
                if path.stat().st_size > self.max_file_bytes:
                    skipped += 1
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw:
                    continue
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and not ("." not in path.name):
                continue
            lines = text.splitlines()
            for start in range(0, len(lines), CHUNK_LINES - CHUNK_OVERLAP):
                body = lines[start:start + CHUNK_LINES]
                if not body:
                    break
                chunks.append({"content": "\n".join(body), "path": path.relative_to(base).as_posix(),
                               "start_line": start + 1, "end_line": start + len(body)})
                if start + CHUNK_LINES >= len(lines):
                    break
        return chunks, skipped

    @staticmethod
    def _embedding_model() -> str:
        return os.getenv("AURORA_CODEBASE_EMBEDDING_MODEL", DEFAULT_CONFIGURATION["embedding.model"])

    def _embed(self, texts: list[str], model: str) -> list[list[float]]:
        return OllamaEmbeddings(model=model).embed_documents(texts)

    def index(self, project_id: str, requested_path: str, *, embedding_model: str | None = None) -> dict[str, int | str]:
        self.validate_project_id(project_id)
        base_root, relative = self._confined_path(requested_path)
        base = (base_root / relative).resolve()
        if not base.is_dir():
            raise RootNotAllowed("path is not allowed")
        lock = self._acquire(project_id)
        try:
            chunks, skipped = self._chunks(base)
            model = embedding_model or self._embedding_model()
            vectors = self._embed([item["content"] for item in chunks], model) if chunks else []
            generation = secrets.token_hex(16)
            payload = {"project_id": project_id, "embedding_model": model,
                       "chunks": [dict(item, embedding=vector) for item, vector in zip(chunks, vectors)]}
            directory = self._project_dir(project_id)
            directory.mkdir(parents=True, exist_ok=True)
            temp = directory / f".generation-{generation}.tmp"
            final = directory / f"generation-{generation}.json"
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, final)
            pointer = directory / "current"
            pointer_temp = directory / f".current-{generation}.tmp"
            pointer_temp.write_text(generation, encoding="ascii")
            os.replace(pointer_temp, pointer)
            return {"project_id": project_id, "indexed_chunks": len(chunks), "skipped_files": skipped}
        finally:
            lock.release()

    def delete(self, project_id: str) -> None:
        import shutil
        lock = self._acquire(project_id)
        try:
            shutil.rmtree(self._project_dir(project_id), ignore_errors=True)
        finally:
            lock.release()

    def projects(self) -> list[str]:
        if not self.root.exists():
            return [LEGACY_PROJECT_ID]
        return [LEGACY_PROJECT_ID] + sorted(item.name for item in self.root.iterdir()
                                            if item.is_dir() and PROJECT_ID_RE.fullmatch(item.name))

    def _published(self, project_id: str) -> dict[str, Any] | None:
        current = self._project_dir(project_id) / "current"
        try:
            generation = current.read_text(encoding="ascii").strip()
            return json.loads((self._project_dir(project_id) / f"generation-{generation}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def search(self, project_id: str, query: str, limit: int, *, embedding_model: str | None = None) -> list[dict[str, Any]]:
        if project_id == LEGACY_PROJECT_ID:
            from langchain_chroma import Chroma

            store = Chroma(
                collection_name="source_code",
                embedding_function=OllamaEmbeddings(model=embedding_model or self._embedding_model()),
                persist_directory="./data/chroma_data",
            )
            return [
                {"content": document.page_content, "path": Path(str(document.metadata.get("source", ""))).name,
                 "start_line": None, "end_line": None, "score": float(score)}
                for document, score in store.similarity_search_with_relevance_scores(query, k=limit)
            ]
        payload = self._published(project_id)
        if payload is None:
            return []
        vectors = self._embed([query], payload["embedding_model"])[0]
        scored = []
        for chunk in payload["chunks"]:
            left, right = vectors, chunk["embedding"]
            denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
            score = sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0
            scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [dict(chunk, score=float(score)) for score, chunk in scored[:limit]]

    def has_published_chunks(self) -> bool:
        return any((self._project_dir(project) / "current").exists() and self._published(project)
                   and self._published(project).get("chunks") for project in self.projects() if project != LEGACY_PROJECT_ID)


def published_chunk_count() -> int:
    try:
        store = CodebaseIndexStore()
        return sum(len(store._published(project).get("chunks", [])) for project in store.projects()
                   if project != LEGACY_PROJECT_ID and store._published(project))
    except OSError:
        return 0
