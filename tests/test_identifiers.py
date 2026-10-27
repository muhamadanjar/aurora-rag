from uuid import RFC_4122

from app.domain.identifiers import new_uuid7
from app.infrastructure.db.models import (
    Configuration,
    DatasetExport,
    TrainingDataset,
    TrainingExample,
    TrainingExampleRevision,
)


def test_new_identifier_is_uuid7():
    identifier = new_uuid7()

    assert identifier.version == 7
    assert identifier.variant == RFC_4122


def test_persistence_model_defaults_are_uuid7():
    identifiers = [
        TrainingDataset().id,
        TrainingExample(dataset_id=new_uuid7()).id,
        TrainingExampleRevision(example_id=new_uuid7()).id,
        DatasetExport(dataset_id=new_uuid7(), requested_by=new_uuid7()).id,
        Configuration(key="test", value="value").id,
    ]

    assert all(identifier.version == 7 for identifier in identifiers)
