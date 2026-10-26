import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DatasetExport, DatasetStatus, ExampleStatus, ExportStatus, RevisionState, ReviewState, TrainingDataset, TrainingExample, TrainingExampleRevision, now
from .settings import Settings

logger = logging.getLogger(__name__)


def validate_chatml(messages: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not messages:
        return ["messages must be a non-empty list"]
    if len(messages) < 2:
        errors.append("messages must contain at least two messages")
    if not isinstance(messages[-1], dict) or messages[-1].get("role") != "assistant":
        errors.append("messages must end with assistant")
    expected = "user"
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != expected:
            errors.append(f"message {index} must have role {expected}")
        if isinstance(message, dict):
            if not isinstance(message.get("content"), str):
                errors.append(f"message {index} content must be a string")
            elif not message["content"].strip():
                errors.append(f"message {index} content must not be empty")
        expected = "assistant" if expected == "user" else "user"
    return errors


async def validate_revision(session: AsyncSession, revision: TrainingExampleRevision, actor: UUID) -> list[str]:
    errors = validate_chatml(revision.content)
    revision.validation_errors = errors or None
    revision.validation_status = RevisionState.valid if not errors else RevisionState.invalid
    revision.validated_by = actor
    revision.validated_at = now()
    revision.updated_at = now()
    revision.version += 1
    return errors


def _safe_export_path(root: Path, path: Path) -> Path:
    root = root.resolve()
    candidate = path.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("export path escapes configured export root")
    return candidate


def _remove_export_artifact(path: Path, export_id: UUID) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:
        logger.exception("failed to remove export artifact", extra={"path": str(path), "export_id": str(export_id)})
        return False

def cleanup_orphaned_export_files(settings: Settings, include_temporary: bool = False) -> int:
    root = settings.export_root.resolve()
    if not root.is_dir():
        return 0
    current = now().timestamp()
    jsonl_cutoff = current - timedelta(days=settings.export_retention_days).total_seconds()
    temp_cutoff = current - timedelta(hours=24).total_seconds()
    removed = 0

    for path in root.glob("*.jsonl"):
        try:
            export_id = UUID(path.stem)
            if path.is_symlink() or not path.is_file() or path.lstat().st_mtime > jsonl_cutoff:
                continue
            safe_path = _safe_export_path(root, path)
            removed += _remove_export_artifact(safe_path, export_id)
        except Exception:
            logger.exception("failed to clean expired export file", extra={"path": str(path)})

    if include_temporary:
        for path in root.glob(".*.tmp"):
            parts = path.name.split(".")
            if len(parts) != 4 or parts[0] or parts[-1] != "tmp":
                continue
            try:
                export_id = UUID(parts[1])
                if path.is_symlink() or not path.is_file() or path.lstat().st_mtime > temp_cutoff:
                    continue
                safe_path = _safe_export_path(root, path)
                removed += _remove_export_artifact(safe_path, export_id)
            except Exception:
                logger.exception("failed to clean stale temporary export file", extra={"path": str(path)})
    return removed


async def cleanup_expired_exports(session: AsyncSession, settings: Settings) -> int:
    cutoff = now()
    result = await session.execute(select(DatasetExport).where(DatasetExport.status == ExportStatus.ready, DatasetExport.expires_at <= cutoff))
    count = 0
    for export in result.scalars():
        deletion_succeeded = True
        if export.object_key:
            try:
                path = _safe_export_path(settings.export_root, settings.export_root / export.object_key)
                deletion_succeeded = _remove_export_artifact(path, export.id)
            except Exception:
                logger.exception("failed to resolve expired export file", extra={"export_id": str(export.id)})
                deletion_succeeded = False
        if not deletion_succeeded:
            continue
        export.status = ExportStatus.expired
        export.expired_at = cutoff
        count += 1
    await session.commit()
    return count


async def reconcile_stale_exports(session: AsyncSession, settings: Settings) -> int:
    cutoff = now() - timedelta(hours=24)
    result = await session.execute(
        select(DatasetExport)
        .where(
            DatasetExport.status.in_((ExportStatus.pending, ExportStatus.failed)),
            DatasetExport.created_at <= cutoff,
        )
        .with_for_update(skip_locked=True)
    )
    root = settings.export_root.resolve()
    count = 0
    for export in result.scalars():
        artifacts = [root / f"{export.id}.jsonl"]
        if root.exists():
            artifacts.extend(root.glob(f".{export.id}.*.tmp"))
        deleted = True
        for artifact in artifacts:
            try:
                safe_path = _safe_export_path(root, artifact)
                deleted = _remove_export_artifact(safe_path, export.id) and deleted
            except Exception:
                logger.exception("failed to reconcile stale export artifact", extra={"export_id": str(export.id), "path": str(artifact)})
                deleted = False
        if not deleted:
            continue
        if export.status == ExportStatus.pending:
            export.status = ExportStatus.failed
            export.error = "stale pending export reconciled"
            export.completed_at = now()
            export.manifest = {**(export.manifest or {}), "status": "failed", "error": export.error}
            count += 1
        if export.object_key is not None:
            export.object_key = None
            count += 1
    await session.commit()
    return count


async def cleanup_loop(session_factory, settings: Settings) -> None:
    while True:
        try:
            await anyio.to_thread.run_sync(cleanup_orphaned_export_files, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("filesystem export cleanup failed")
        try:
            async with session_factory() as session:
                exports_table = await session.scalar(text("SELECT to_regclass('dataset_exports')"))
                if exports_table is None:
                    await anyio.to_thread.run_sync(cleanup_orphaned_export_files, settings, True)
                else:
                    await reconcile_stale_exports(session, settings)
                    await cleanup_expired_exports(session, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("database-backed export cleanup failed")
        await asyncio.sleep(settings.cleanup_interval_seconds)


async def _persist_failed_export(
    session: AsyncSession,
    export_id: UUID,
    dataset_id: UUID,
    dataset_version: int,
    settings: Settings,
    temp_path: Path | None,
    final_path: Path | None,
    error: BaseException,
) -> None:
    remaining: list[Path] = []
    for path in (temp_path, final_path):
        if path is None:
            continue
        try:
            safe_path = _safe_export_path(settings.export_root, path)
        except Exception:
            logger.exception("failed to resolve export artifact during failure cleanup", extra={"export_id": str(export_id), "path": str(path)})
            remaining.append(path)
            continue
        if not _remove_export_artifact(safe_path, export_id):
            remaining.append(safe_path)
    await session.rollback()
    export = await session.get(DatasetExport, export_id)
    if export is None:
        logger.error("failed export row disappeared before failure metadata was persisted", extra={"export_id": str(export_id)})
        return
    export.status = ExportStatus.failed
    export.object_key = None
    if remaining:
        try:
            export.object_key = remaining[0].relative_to(settings.export_root.resolve()).as_posix()
        except ValueError:
            logger.error("failed export artifact is outside export root", extra={"export_id": str(export_id)})
    export.error = str(error)
    export.completed_at = now()
    export.manifest = {"dataset_id": str(dataset_id), "version": dataset_version, "status": "failed", "error": export.error}
    try:
        await session.commit()
    except Exception:
        logger.exception("failed to persist failed export metadata", extra={"export_id": str(export_id)})


async def create_export(session: AsyncSession, dataset: TrainingDataset, actor: UUID, settings: Settings) -> DatasetExport:
    dataset_id = dataset.id
    dataset_version = dataset.version
    export = DatasetExport(dataset_id=dataset_id, requested_by=actor, status=ExportStatus.pending)
    export.manifest = {"dataset_id": str(dataset_id), "version": dataset_version, "status": "pending"}
    session.add(export)
    await session.flush()
    export_id = export.id
    await session.commit()
    digest = hashlib.sha256()
    revision_ids: list[str] = []
    record_count = 0
    stream = None
    temp_path: Path | None = None
    final_path: Path | None = None
    try:
        locked_export = await session.scalar(select(DatasetExport).where(DatasetExport.id == export_id).with_for_update())
        if not locked_export or locked_export.status != ExportStatus.pending:
            raise ValueError("export is no longer pending")
        settings.export_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(settings.export_root, 0o700)
        final_path = _safe_export_path(settings.export_root, settings.export_root / f"{export_id}.jsonl")
        locked_dataset = await session.scalar(select(TrainingDataset).where(TrainingDataset.id == dataset_id).with_for_update())
        if not locked_dataset or locked_dataset.status != DatasetStatus.active:
            raise ValueError("dataset is no longer active")
        dataset_version = locked_dataset.version
        temp_fd, temp_name = tempfile.mkstemp(prefix=f".{export_id}.", suffix=".tmp", dir=settings.export_root)
        temp_path = Path(temp_name)
        os.chmod(temp_path, 0o600)
        os.close(temp_fd)
        async with await anyio.open_file(temp_path, "wb") as handle:
            stream = await session.stream_scalars(
                select(TrainingExampleRevision)
                .join(TrainingExample, TrainingExample.id == TrainingExampleRevision.example_id)
                .where(
                    TrainingExample.dataset_id == dataset_id,
                    TrainingExample.status == ExampleStatus.active,
                    TrainingExampleRevision.id == TrainingExample.current_revision_id,
                    TrainingExampleRevision.validation_status == RevisionState.valid,
                    TrainingExampleRevision.review_status == ReviewState.approved,
                )
                .order_by(TrainingExampleRevision.id)
            )
            async for revision in stream:
                record = {"messages": revision.content}
                if revision.system_prompt:
                    record["messages"] = [{"role": "system", "content": revision.system_prompt}, *record["messages"]]
                data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                await handle.write(data)
                digest.update(data)
                revision_ids.append(str(revision.id))
                record_count += 1
        if not record_count:
            raise ValueError("cannot export a dataset with no eligible revisions")
        os.replace(temp_path, final_path)
        os.utime(final_path, None)
        export = locked_export
        export.status = ExportStatus.ready
        export.object_key = final_path.relative_to(settings.export_root.resolve()).as_posix()
        export.checksum_sha256 = digest.hexdigest()
        export.item_count = record_count
        export.error = None
        export.completed_at = now()
        export.manifest = {
            "dataset_id": str(dataset_id),
            "version": dataset_version,
            "format_type": locked_dataset.format_type,
            "export_schema_version": "1",
            "serialization": "jsonl",
            "chatml": True,
            "checksum_sha256": export.checksum_sha256,
            "item_count": record_count,
            "revision_ids": revision_ids,
        }
        export.expires_at = now() + timedelta(days=settings.export_retention_days)
        await session.commit()
        await session.refresh(export)
        return export
    except asyncio.CancelledError as exc:
        await asyncio.shield(_persist_failed_export(session, export_id, dataset_id, dataset_version, settings, temp_path, final_path, exc))
        raise
    except Exception as exc:
        await _persist_failed_export(session, export_id, dataset_id, dataset_version, settings, temp_path, final_path, exc)
        raise
    finally:
        if stream is not None:
            try:
                await asyncio.shield(stream.close())
            except Exception:
                logger.exception("failed to close export result cursor", extra={"export_id": str(export_id)})
