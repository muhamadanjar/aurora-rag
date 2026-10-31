"""Durable codebase index worker entry point (``python -m app.worker``)."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os

from sqlalchemy import select, text

from app.application.rag.config_service import load_configuration
from app.infrastructure.codebase.index_store import (
    CodebaseIndexError, CodebaseIndexStore, InvalidProject, RootNotAllowed,
    ProjectBusy, ScanError,
)
from app.infrastructure.codebase.jobs import (
    HEARTBEAT_INTERVAL, JobFenced, JobLease, claim_job, cleanup_terminal_jobs,
)
from app.infrastructure.db.connection import SessionLocal, engine
from app.infrastructure.db.models import IndexJob, IndexJobStage, IndexJobStatus, now
from app.infrastructure.db.repositories.configuration_repo import SqlConfigurationRepository

log = logging.getLogger("aurora.index_worker")
MODEL_LOCK_BACKOFF_SECONDS = max(1, int(os.getenv("INDEX_MODEL_LOCK_BACKOFF_SECONDS", "2")))
MODEL_LOCK_BACKOFF_CAP_SECONDS = 30


def error_category(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, (InvalidProject, RootNotAllowed)):
        return "invalid_source", "Sumber codebase tidak valid"
    if isinstance(exc, ScanError):
        return "scan_failed", "Pemindaian codebase gagal"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return "embedding_unavailable", "Layanan embedding tidak dapat dihubungi"
    if isinstance(exc, CodebaseIndexError):
        return "storage_failed", "Penyimpanan index gagal"
    if isinstance(exc, JobFenced):
        return "interrupted", "Eksekusi job dihentikan"
    return "internal_error", "Indexing codebase gagal"


async def _finish(lease: JobLease, *, status: IndexJobStatus, code: str | None = None,
                  message: str | None = None, result: dict | None = None) -> None:
    if not await lease.finish(status=status, code=code, message=message, result=result):
        raise JobFenced("indexing job ownership was lost")


async def execute_job(lease: JobLease) -> bool:
    loop = asyncio.get_running_loop()
    heartbeat_task = asyncio.create_task(lease.heartbeat())
    try:
        settings_session = SessionLocal()
        repository = SqlConfigurationRepository(settings_session)
        store = CodebaseIndexStore()
        try:
            model_lock = await asyncio.to_thread(store.acquire_model_lock)
            configuration = await load_configuration(repository)
        finally:
            await settings_session.close()

        def persist_stage(stage: str) -> None:
            lease.assert_owned()
            lease.job.stage = IndexJobStage(stage)
            future = asyncio.run_coroutine_threadsafe(
                lease.progress(stage=IndexJobStage(stage)), loop)
            if not future.result(timeout=30):
                lease.lost.set()
                raise JobFenced("indexing job ownership was lost")

        def persist_progress(processed: int, total: int, skipped: int) -> None:
            lease.assert_owned()
            future = asyncio.run_coroutine_threadsafe(
                lease.progress(processed_chunks=processed, total_chunks=total,
                               skipped_files=skipped), loop)
            if not future.result(timeout=30):
                lease.lost.set()
                raise JobFenced("indexing job ownership was lost")

        def confirm_live_sync() -> None:
            future = asyncio.run_coroutine_threadsafe(lease.confirm_live(), loop)
            if not future.result(timeout=30):
                lease.lost.set()
                raise JobFenced("indexing job ownership was lost")

        result = await asyncio.to_thread(
            store.index, lease.job.project_id, lease.job.source_path,
            embedding_model=configuration["embedding.model"], model_lock_held=True,
            job_id=str(lease.job.id), ownership_check=confirm_live_sync,
            stage_callback=persist_stage, progress_callback=persist_progress,
        )
        lease.assert_owned()
        await _finish(lease, status=IndexJobStatus.succeeded, result=result)
        log.info("index job finished", extra={"job_id": str(lease.job.id), "status": "succeeded"})
    except ProjectBusy:
        # The embedding model lock is global. Leave this job queued for another
        # worker instead of turning cross-project contention into a failure.
        try:
            await lease.requeue()
        finally:
            log.info("index job deferred", extra={"job_id": str(lease.job.id), "reason": "model_lock_busy"})
        return True
    except Exception as exc:
        code, message = error_category(exc)
        if lease.job.stage is IndexJobStage.embedding and code == "internal_error":
            code, message = "embedding_unavailable", "Layanan embedding tidak dapat dihubungi"
        try:
            await _finish(lease, status=IndexJobStatus.failed, code=code, message=message)
        except Exception:
            log.warning("index job status could not be persisted", extra={"job_id": str(lease.job.id), "category": code})
    finally:
        if "model_lock" in locals():
            model_lock.release()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await lease.close()
    return False


async def reconcile_running_jobs() -> None:
    """Fence dead workers; a live worker keeps its PostgreSQL advisory lock."""
    connection = await engine().connect()
    try:
        rows = (await connection.execute(
            select(IndexJob.__table__).where(IndexJob.__table__.c.status == IndexJobStatus.running)
        )).mappings().all()
        for job in rows:
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": job.project_id}
            )
            if not locked:
                continue
            store = CodebaseIndexStore()
            project_lock = None
            publication_fd = None
            try:
                try:
                    project_lock = await asyncio.to_thread(store._acquire, job["project_id"])
                except ProjectBusy:
                    continue
                publication_fd = store._open_flock(store._publication_lock_path(job["project_id"]))
                try:
                    fcntl.flock(publication_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                await connection.execute(
                    text("UPDATE index_jobs SET execution_id=NULL "
                         "WHERE id=:id AND status='running'"),
                    {"id": job["id"]},
                )
                await connection.commit()
                publication = store._published(job["project_id"])
                if publication and publication.get("job_id") == str(job["id"]):
                    await connection.execute(
                        text("UPDATE index_jobs SET status='succeeded', stage='publish', "
                             "indexed_chunks=:indexed_chunks, skipped_files=:skipped_files, "
                             "finished_at=:finished_at, error_code=NULL, error_message=NULL "
                             "WHERE id=:id AND status='running' AND execution_id IS NULL"),
                        {"indexed_chunks": store._client().get_collection(publication["collection_name"]).count(),
                         "skipped_files": publication.get("skipped_files", job["skipped_files"]),
                         "finished_at": now(), "id": job["id"]},
                    )
                else:
                    await connection.execute(
                        text("UPDATE index_jobs SET status='failed', error_code='interrupted', "
                             "error_message='Eksekusi job terputus', finished_at=:finished_at "
                             "WHERE id=:id AND status='running' AND execution_id IS NULL"),
                        {"finished_at": now(), "id": job["id"]},
                    )
                await connection.commit()
            finally:
                if publication_fd is not None:
                    try:
                        fcntl.flock(publication_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(publication_fd)
                if project_lock is not None:
                    project_lock.release()
                await connection.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": job["project_id"]})
                await connection.commit()
    finally:
        await connection.close()


async def run_worker() -> None:
    model_lock_backoff = MODEL_LOCK_BACKOFF_SECONDS
    while True:
        await reconcile_running_jobs()
        lease = await claim_job(engine().connect)
        if lease is not None:
            deferred = await execute_job(lease)
            if deferred:
                await asyncio.sleep(model_lock_backoff)
                model_lock_backoff = min(
                    MODEL_LOCK_BACKOFF_CAP_SECONDS, model_lock_backoff * 2
                )
            else:
                model_lock_backoff = MODEL_LOCK_BACKOFF_SECONDS
            continue
        session = SessionLocal()
        try:
            await cleanup_terminal_jobs(session)
        finally:
            await session.close()
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
