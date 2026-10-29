"""support Alpaca training datasets"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_training_datasets_format_type_chatml",
        "training_datasets",
        type_="check",
    )
    op.create_check_constraint(
        "ck_training_datasets_format_type_supported",
        "training_datasets",
        "format_type IN ('chatml', 'alpaca')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    has_alpaca = connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM training_datasets WHERE format_type = 'alpaca')")
    )
    if has_alpaca:
        raise RuntimeError(
            "Cannot downgrade while Alpaca datasets exist; export or remove them first"
        )
    op.drop_constraint(
        "ck_training_datasets_format_type_supported",
        "training_datasets",
        type_="check",
    )
    op.create_check_constraint(
        "ck_training_datasets_format_type_chatml",
        "training_datasets",
        "format_type = 'chatml'",
    )
