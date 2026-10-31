"""persisted codebase indexing jobs"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status = postgresql.ENUM("queued", "running", "succeeded", "failed", name="index_job_status", create_type=False)
    stage = postgresql.ENUM("validation", "scan", "create_collection", "embedding", "store_batch", "publish", name="index_job_stage", create_type=False)
    status.create(op.get_bind(), checkfirst=True)
    stage.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "index_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("stage", stage, nullable=True),
        sa.Column("processed_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_chunks", sa.Integer(), nullable=True),
        sa.Column("skipped_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_chunks", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=256), nullable=True),
        sa.Column("execution_id", sa.String(length=128), nullable=True),
        sa.Column("worker_id", sa.String(length=256), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_index_jobs_project_created", "index_jobs", ["project_id", "created_at"])
    op.create_index("ix_index_jobs_queue", "index_jobs", ["status", "created_at"])
    op.create_index(
        "uq_index_jobs_active_project", "index_jobs", ["project_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_index_jobs_active_project", table_name="index_jobs")
    op.drop_index("ix_index_jobs_queue", table_name="index_jobs")
    op.drop_index("ix_index_jobs_project_created", table_name="index_jobs")
    op.drop_table("index_jobs")
    sa.Enum(name="index_job_stage").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="index_job_status").drop(op.get_bind(), checkfirst=True)
