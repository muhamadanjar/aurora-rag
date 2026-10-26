import asyncio
import pytest
import bcrypt

from dataset_management import service
from dataset_management.models import DatasetExport, DatasetStatus, RevisionState, ReviewState, TrainingDataset, TrainingExampleRevision
from dataset_management.settings import Settings
from uuid import uuid4


class FakeStream:
    def __init__(self, items):
        self.items = items
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for item in self.items:
            yield item

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, revision, dataset):
        self.revision = revision
        self.dataset = dataset
        self.export = None
        self.rolled_back = False

    async def scalar(self, query):
        entity = query.column_descriptions[0]["entity"]
        return self.export if entity is DatasetExport else self.dataset

    async def stream_scalars(self, _query):
        return FakeStream([self.revision])

    async def get(self, _model, _key):
        return self.export

    def add(self, value):
        self.export = value

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _value):
        return None

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_export_failure_cleanup_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(service.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("publish failed")))
    revision = TrainingExampleRevision(example_id=uuid4(), revision_number=1, version=1, content=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], validation_status=RevisionState.valid, review_status=ReviewState.approved, created_by=uuid4())
    dataset = TrainingDataset(title="failure", created_by=uuid4(), updated_by=uuid4(), status=DatasetStatus.active)
    session = FakeSession(revision, dataset)
    settings = Settings(database_url="postgresql+asyncpg://db", auth_username="operator", auth_password_hash=bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode(), jwt_secret="x" * 32, operator_uuid=uuid4(), export_root=tmp_path)
    with pytest.raises(OSError):
        await service.create_export(session, dataset, uuid4(), settings)
    assert session.rolled_back
    assert session.export.status.value == "failed"
    assert session.export.manifest["status"] == "failed"
    assert list(tmp_path.iterdir()) == []



@pytest.mark.asyncio
async def test_export_cancellation_persists_failed_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(service.os, "replace", lambda *_: (_ for _ in ()).throw(asyncio.CancelledError()))
    revision = TrainingExampleRevision(example_id=uuid4(), revision_number=1, version=1, content=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], validation_status=RevisionState.valid, review_status=ReviewState.approved, created_by=uuid4())
    dataset = TrainingDataset(title="cancel", created_by=uuid4(), updated_by=uuid4(), status=DatasetStatus.active)
    session = FakeSession(revision, dataset)
    settings = Settings(database_url="postgresql+asyncpg://db", auth_username="operator", auth_password_hash=bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode(), jwt_secret="x" * 32, operator_uuid=uuid4(), export_root=tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await service.create_export(session, dataset, uuid4(), settings)
    assert session.export.status.value == "failed"

@pytest.mark.asyncio
async def test_export_root_failure_persists_failed_metadata(tmp_path):
    revision = TrainingExampleRevision(example_id=uuid4(), revision_number=1, version=1, content=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], validation_status=RevisionState.valid, review_status=ReviewState.approved, created_by=uuid4())
    dataset = TrainingDataset(title="unwritable", created_by=uuid4(), updated_by=uuid4(), status=DatasetStatus.active)
    root_is_file = tmp_path / "not-a-directory"
    root_is_file.write_text("blocking directory creation")
    session = FakeSession(revision, dataset)
    settings = Settings(database_url="postgresql+asyncpg://db", auth_username="operator", auth_password_hash=bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode(), jwt_secret="x" * 32, operator_uuid=uuid4(), export_root=root_is_file)
    with pytest.raises(FileExistsError):
        await service.create_export(session, dataset, uuid4(), settings)
    assert session.export.status.value == "failed"
    assert session.export.manifest["status"] == "failed"
    assert session.rolled_back
