import hashlib
import json
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import anyio

from app.core.config import Settings
from app.core.exceptions import StoragePathViolation

logger = logging.getLogger(__name__)


def safe_export_path(root: Path, path: Path) -> Path:
    root = root.resolve()
    candidate = path.resolve()
    if candidate != root and root not in candidate.parents:
        raise StoragePathViolation("export path escapes configured export root")
    return candidate


def remove_export_artifact(path: Path, export_id: UUID) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:
        logger.exception("failed to remove export artifact", extra={"path": str(path), "export_id": str(export_id)})
        return False


def cleanup_orphaned_export_files(settings: Settings, include_temporary: bool = False) -> int:
    from datetime import datetime, timezone
    root = settings.export_root.resolve()
    if not root.is_dir():
        return 0
    current = datetime.now(timezone.utc).timestamp()
    jsonl_cutoff = current - timedelta(days=settings.export_retention_days).total_seconds()
    temp_cutoff = current - timedelta(hours=24).total_seconds()
    removed = 0

    for path in root.glob("*.jsonl"):
        try:
            parts = path.stem.split(".")
            if len(parts) != 1:
                continue
            UUID(parts[0])
            if path.is_symlink() or not path.is_file() or path.lstat().st_mtime > jsonl_cutoff:
                continue
            safe = safe_export_path(root, path)
            removed += remove_export_artifact(safe, UUID(parts[0]))
        except Exception:
            logger.exception("failed to clean orphaned export file", extra={"path": str(path)})

    if include_temporary:
        for path in root.glob(".*.tmp"):
            parts = path.name.split(".")
            if len(parts) != 4 or parts[0] or parts[-1] != "tmp":
                continue
            try:
                export_id = UUID(parts[1])
                if path.is_symlink() or not path.is_file() or path.lstat().st_mtime > temp_cutoff:
                    continue
                safe = safe_export_path(root, path)
                removed += remove_export_artifact(safe, export_id)
            except Exception:
                logger.exception("failed to clean stale temporary export file", extra={"path": str(path)})

    return removed


async def write_export_file(
    export_root: Path,
    export_id: UUID,
    revisions,  # async generator
) -> tuple[Path, str, list[str], int]:
    """
    Stream revisions → temp file → atomic rename to final path.
    Returns (final_path, sha256_hex, revision_ids, record_count).
    """
    export_root.mkdir(parents=True, exist_ok=True)
    final_path = export_root / f"{export_id}.jsonl"
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{export_id}.", suffix=".tmp", dir=export_root)
    temp_path = Path(temp_name)
    os.chmod(temp_path, 0o600)
    os.close(temp_fd)

    digest = hashlib.sha256()
    revision_ids: list[str] = []
    record_count = 0

    async with await anyio.open_file(temp_path, "wb") as handle:
        async for revision in revisions:
            record = {"messages": revision.content}
            if revision.system_prompt:
                record["messages"] = [{"role": "system", "content": revision.system_prompt}, *record["messages"]]
            data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            await handle.write(data)
            digest.update(data)
            revision_ids.append(str(revision.id))
            record_count += 1

    if not record_count:
        temp_path.unlink(missing_ok=True)
        raise ValueError("cannot export a dataset with no eligible revisions")

    os.replace(temp_path, final_path)
    return final_path, digest.hexdigest(), revision_ids, record_count
