from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from dataset_management.models import SQLModel
from dataset_management.settings import get_settings

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = SQLModel.metadata


def run_migrations_offline():
    context.configure(url=get_settings().database_url.replace("+asyncpg", ""), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction(): context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
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
