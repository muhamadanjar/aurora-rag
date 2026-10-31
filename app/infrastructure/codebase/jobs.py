"""Durable queue, fencing, recovery, and retention for codebase indexing."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.infrastructure.db.models import IndexJob, IndexJobStage, IndexJobStatus, now

JOB_RETENTION = timedelta(days=30)
HEARTBEAT_INTERVAL = max(1, int(os.getenv("INDEX_WORKER_HEARTBEAT_SECONDS", "5")))


class JobFenced(RuntimeError):
    """The worker no longer owns the durable job lease."""


class ActiveJobConflict(RuntimeError):
    """Only one queued/running job is permitted per project."""


@dataclass
class ClaimedJob:
    id: uuid.UUID
    project_id: str
    source_path: str
    execution_id: str
    stage: IndexJobStage
    processed_chunks: int = 0
    total_chunks: int | None = None
    skipped_files: int = 0


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


async def enqueue(session: AsyncSession, project_id: str, source_path: str) -> IndexJob:
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": project_id}
    )
    if not locked:
        await session.rollback()
        raise ActiveJobConflict("project has an active indexing job")
    job = IndexJob(project_id=project_id, source_path=source_path)
    session.add(job)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ActiveJobConflict("project has an active indexing job") from exc
    await session.refresh(job)
    return job


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> IndexJob | None:
    return await session.get(IndexJob, job_id)


async def update_job(session: AsyncSession, job_id: uuid.UUID, execution_id: str, **values: Any) -> bool:
    result = await session.execute(
        update(IndexJob).where(IndexJob.id == job_id, IndexJob.execution_id == execution_id)
        .values(**values)
    )
    await session.commit()
    return result.rowcount == 1


async def set_progress(
    session: AsyncSession, job_id: uuid.UUID, execution_id: str, *,
    stage: IndexJobStage | str | None = None, processed_chunks: int | None = None,
    total_chunks: int | None = None, skipped_files: int | None = None,
) -> bool:
    values: dict[str, Any] = {"heartbeat_at": now()}
    if stage is not None:
        values["stage"] = stage
    if processed_chunks is not None:
        values["processed_chunks"] = processed_chunks
    if total_chunks is not None:
        values["total_chunks"] = total_chunks
    if skipped_files is not None:
        values["skipped_files"] = skipped_files
    return await update_job(session, job_id, execution_id, **values)


async def cleanup_terminal_jobs(session: AsyncSession) -> int:
    result = await session.execute(
        text("DELETE FROM index_jobs WHERE status IN ('succeeded', 'failed') "
             "AND finished_at < now() - interval '30 days'")
    )
    await session.commit()
    return result.rowcount


async def run_delete_with_project_lease(
    project_id: str, delete_operation: Callable[[], Any], connection_factory: Callable[[], Any],
) -> None:
    """Hold the project advisory lock on one pinned backend while deleting Chroma files."""
    connection: AsyncConnection = await connection_factory()
    try:
        locked = await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": project_id}
        )
        if not locked:
            raise ActiveJobConflict("project operation already in progress")
        active = await connection.scalar(
            text("SELECT 1 FROM index_jobs WHERE project_id=:project_id "
                 "AND status IN ('queued','running') LIMIT 1"),
            {"project_id": project_id},
        )
        await connection.commit()
        if active:
            raise ActiveJobConflict("project has an active indexing job")
        await asyncio.to_thread(delete_operation)
    finally:
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": project_id}
            )
            await connection.commit()
        except Exception:
            pass
        await connection.close()


class JobLease:
    """A pinned PostgreSQL backend advisory lock plus a fenced row owner."""

    def __init__(self, connection: AsyncConnection, job: ClaimedJob,
                 connection_factory: Callable[[], Any]):
        self.connection = connection
        self.job = job
        self.connection_factory = connection_factory
        self.execution_id = job.execution_id
        self.lost = asyncio.Event()
        self.db_lock = asyncio.Lock()

    async def heartbeat(self) -> None:
        while not self.lost.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self.lost.is_set():
                return
            try:
                async with self.db_lock:
                    result = await self.connection.execute(
                        update(IndexJob.__table__).where(
                            IndexJob.__table__.c.id == self.job.id,
                            IndexJob.__table__.c.execution_id == self.execution_id,
                        ).values(heartbeat_at=now())
                    )
                    await self.connection.commit()
                    ok = result.rowcount == 1
            except Exception:
                ok = False
            if not ok:
                self.lost.set()
                await self.revoke_execution()

    async def revoke_execution(self) -> None:
        """Fence this owner from a fresh backend connection after lease loss."""
        try:
            connection: AsyncConnection = await self.connection_factory()
            try:
                await connection.execute(
                    update(IndexJob.__table__).where(
                        IndexJob.__table__.c.id == self.job.id,
                        IndexJob.__table__.c.execution_id == self.execution_id,
                        IndexJob.__table__.c.status == IndexJobStatus.running,
                    ).values(execution_id=None)
                )
                await connection.commit()
            finally:
                await connection.close()
        except Exception:
            # Publication fails closed through confirm_live when the database is unavailable.
            return

    async def confirm_live(self) -> bool:
        if self.lost.is_set():
            return False
        connection: AsyncConnection | None = None
        try:
            connection = await self.connection_factory()
            current = await connection.scalar(
                select(IndexJob.__table__.c.execution_id).where(
                    IndexJob.__table__.c.id == self.job.id,
                    IndexJob.__table__.c.status == IndexJobStatus.running,
                )
            )
            return current == self.execution_id
        except Exception:
            self.lost.set()
            await self.revoke_execution()
            return False
        finally:
            if connection is not None:
                await connection.close()

    async def requeue(self) -> None:
        async with self.db_lock:
            await self.connection.execute(
                update(IndexJob.__table__).where(
                    IndexJob.__table__.c.id == self.job.id,
                    IndexJob.__table__.c.execution_id == self.execution_id,
                    IndexJob.__table__.c.status == IndexJobStatus.running,
                ).values(
                    status=IndexJobStatus.queued, stage=None, execution_id=None,
                    worker_id=None, started_at=None, heartbeat_at=None,
                )
            )
            await self.connection.commit()

    async def progress(self, **values: Any) -> bool:
        if "processed_chunks" in values:
            self.job.processed_chunks = values["processed_chunks"]
        if "total_chunks" in values:
            self.job.total_chunks = values["total_chunks"]
        if "skipped_files" in values:
            self.job.skipped_files = values["skipped_files"]
        if "stage" in values:
            self.job.stage = values["stage"]
        try:
            async with self.db_lock:
                result = await self.connection.execute(
                    update(IndexJob.__table__).where(
                        IndexJob.__table__.c.id == self.job.id,
                        IndexJob.__table__.c.execution_id == self.execution_id,
                    ).values(heartbeat_at=now(), **values)
                )
                await self.connection.commit()
                ok = result.rowcount == 1
        except Exception:
            self.lost.set()
            await self.revoke_execution()
            raise
        if not ok:
            self.lost.set()
            await self.revoke_execution()
        return ok

    async def finish(self, *, status: IndexJobStatus, code: str | None,
                     message: str | None, result: dict | None) -> bool:
        values: dict[str, Any] = {
            "status": status, "finished_at": now(), "heartbeat_at": now(),
            "error_code": code, "error_message": message,
            "processed_chunks": self.job.processed_chunks,
            "total_chunks": self.job.total_chunks,
            "skipped_files": result.get("skipped_files", self.job.skipped_files) if result else self.job.skipped_files,
        }
        if result:
            values["indexed_chunks"] = result["indexed_chunks"]
        async with self.db_lock:
            result_row = await self.connection.execute(
                update(IndexJob.__table__).where(
                    IndexJob.__table__.c.id == self.job.id,
                    IndexJob.__table__.c.execution_id == self.execution_id,
                ).values(**values)
            )
            await self.connection.commit()
        return result_row.rowcount == 1

    def assert_owned(self) -> None:
        if self.lost.is_set():
            raise JobFenced("indexing job ownership was lost")

    async def close(self) -> None:
        self.lost.set()
        try:
            await self.connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": self.job.project_id},
            )
            await self.connection.commit()
        except Exception:
            # Closing the pinned backend connection releases the advisory lock.
            pass
        finally:
            await self.connection.close()


async def claim_job(connection_factory: Callable[[], Any]) -> JobLease | None:
    """Claim one queued job; a dead worker's advisory lock is automatically released."""
    connection: AsyncConnection = await connection_factory()
    try:
        claimed: ClaimedJob | None = None
        async with connection.begin():
            result = await connection.execute(
                select(IndexJob.__table__).where(IndexJob.__table__.c.status == IndexJobStatus.queued)
                .order_by(IndexJob.__table__.c.created_at).with_for_update(skip_locked=True).limit(1)
            )
            row = result.mappings().first()
            if row is not None:
                locked = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                    {"key": row["project_id"]},
                )
                if locked:
                    execution_id = str(uuid.uuid4())
                    await connection.execute(
                        update(IndexJob.__table__).where(IndexJob.__table__.c.id == row["id"]).values(
                            status=IndexJobStatus.running, stage=IndexJobStage.validation,
                            execution_id=execution_id, worker_id=worker_id(),
                            started_at=now(), heartbeat_at=now(),
                        )
                    )
                    claimed = ClaimedJob(
                        id=row["id"], project_id=row["project_id"], source_path=row["source_path"],
                        execution_id=execution_id, stage=IndexJobStage.validation,
                    )
        if claimed is None:
            await connection.close()
            return None
        return JobLease(connection, claimed, connection_factory)
    except Exception:
        await connection.rollback()
        await connection.close()
        raise
