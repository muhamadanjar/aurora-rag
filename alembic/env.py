from logging.config import fileConfig
import os
import re

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.infrastructure.db.models import SQLModel
from app.core.config import get_settings

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = SQLModel.metadata


def get_next_revision_id() -> str:
    """Return the next four-digit migration number from existing revisions."""
    versions_dir = os.path.join(os.path.dirname(__file__), "versions")
    numbers = []
    for filename in os.listdir(versions_dir):
        match = re.match(r"^(\d+)_.*\.py$", filename)
        if match:
            numbers.append(int(match.group(1)))
    return f"{(max(numbers) if numbers else 0) + 1:04d}"


def process_revision_directives(_context, _revision, directives):
    """Assign sequential revision IDs to newly generated migrations."""
    for directive in directives or []:
        if directive.upgrade_ops.is_empty():
            continue
        directive.revision = get_next_revision_id()
        directive.branch_labels = None


def run_migrations_offline():
    context.configure(
        url=get_settings().database_url.replace("+asyncpg", ""),
        target_metadata=target_metadata,
        literal_binds=True,
        process_revision_directives=process_revision_directives,
    )
    with context.begin_transaction(): context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        process_revision_directives=process_revision_directives,
    )
    with context.begin_transaction(): context.run_migrations()


async def run_migrations_online():
    connectable = async_engine_from_config({"sqlalchemy.url": get_settings().database_url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode(): run_migrations_offline()
else:
    import asyncio
    asyncio.run(run_migrations_online())
