"""accepted dataset management schema"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()
    dataset_status = postgresql.ENUM("active", "archived", name="dataset_status")
    example_status = postgresql.ENUM("active", "retired", name="example_status")
    validation_status = postgresql.ENUM("pending", "valid", "invalid", name="revision_validation_status")
    review_status = postgresql.ENUM("pending", "approved", "rejected", name="revision_review_status")
    export_status = postgresql.ENUM("pending", "ready", "failed", "expired", name="export_status")
    for enum in (dataset_status, example_status, validation_status, review_status, export_status):
        enum.create(op.get_bind(), checkfirst=True)
    op.create_table("training_datasets",
        sa.Column("id", uuid, primary_key=True), sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()), sa.Column("format_type", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", postgresql.ENUM("active", "archived", name="dataset_status", create_type=False), nullable=False), sa.Column("created_by", uuid, nullable=False), sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("format_type = 'chatml'", name="ck_training_datasets_format_type_chatml"))
    op.create_table("training_examples",
        sa.Column("id", uuid, primary_key=True), sa.Column("dataset_id", uuid, sa.ForeignKey("training_datasets.id", name="fk_training_examples_dataset_id"), nullable=False),
        sa.Column("status", postgresql.ENUM("active", "retired", name="example_status", create_type=False), nullable=False), sa.Column("current_revision_id", uuid), sa.Column("version", sa.Integer(), nullable=False), sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("retired_at", sa.DateTime(timezone=True)))
    op.create_table("training_example_revisions",
        sa.Column("id", uuid, primary_key=True), sa.Column("example_id", uuid, sa.ForeignKey("training_examples.id", name="fk_training_example_revisions_example_id"), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("content", jsonb, nullable=False), sa.Column("system_prompt", sa.Text()),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("validation_status", postgresql.ENUM("pending", "valid", "invalid", name="revision_validation_status", create_type=False), nullable=False), sa.Column("review_status", postgresql.ENUM("pending", "approved", "rejected", name="revision_review_status", create_type=False), nullable=False),
        sa.Column("validation_errors", jsonb), sa.Column("created_by", uuid, nullable=False), sa.Column("validated_by", uuid), sa.Column("reviewed_by", uuid),
        sa.Column("review_note", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True)), sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("example_id", "revision_number", name="uq_training_example_revisions_example_revision_number"))
    op.create_foreign_key("fk_training_examples_current_revision_id", "training_examples", "training_example_revisions", ["current_revision_id"], ["id"], deferrable=True, initially="DEFERRED")
    op.create_table("dataset_exports",
        sa.Column("id", uuid, primary_key=True), sa.Column("dataset_id", uuid, sa.ForeignKey("training_datasets.id", name="fk_dataset_exports_dataset_id"), nullable=False),
        sa.Column("status", postgresql.ENUM("pending", "ready", "failed", "expired", name="export_status", create_type=False), nullable=False), sa.Column("manifest", jsonb, nullable=False),
        sa.Column("requested_by", uuid, nullable=False), sa.Column("object_key", sa.Text()), sa.Column("checksum_sha256", sa.String(64)), sa.Column("item_count", sa.Integer()), sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("expires_at", sa.DateTime(timezone=True)), sa.Column("expired_at", sa.DateTime(timezone=True)))
    op.create_index("ix_training_examples_dataset_created", "training_examples", ["dataset_id", "created_at", "id"])
    op.create_index("ix_training_example_revisions_example_created", "training_example_revisions", ["example_id", "created_at"])
    op.create_index("ix_dataset_exports_dataset_created", "dataset_exports", ["dataset_id", "created_at"])

def downgrade():
    op.drop_index("ix_dataset_exports_dataset_created", table_name="dataset_exports")
    op.drop_index("ix_training_example_revisions_example_created", table_name="training_example_revisions")
    op.drop_index("ix_training_examples_dataset_created", table_name="training_examples")
    op.drop_table("dataset_exports")
    op.drop_constraint("fk_training_examples_current_revision_id", "training_examples", type_="foreignkey")
    op.drop_table("training_example_revisions")
    op.drop_table("training_examples")
    op.drop_table("training_datasets")

