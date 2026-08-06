"""Alembic environment.

Migrations run through the same asyncpg driver the application uses, so there is
only one Postgres driver in the dependency tree. The database URL always comes
from application settings rather than alembic.ini.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.db.async_url)

target_metadata = Base.metadata


# Indexes Alembic cannot reflect back into a form comparable with the model
# metadata. Left in the comparison they would be reported as spurious drift on
# every single run, which trains everyone to ignore `alembic check` — the one
# thing that must stay trustworthy. Each is created explicitly in a migration
# and must be changed there.
#
#   * ix_chunk_body_fts             expression index, to_tsvector(...)
#   * uq_embedding_collection_...   partial index, WHERE status = 'active'
#   * ix_chunk_embedding_*_hnsw_*   HNSW access method with a vector ops class
UNCOMPARABLE_INDEXES = frozenset(
    {
        "ix_chunk_body_fts",
        "uq_embedding_collection_single_active",
    }
    | {
        f"ix_chunk_embedding_{dimensions}_hnsw_{metric}"
        for dimensions in (384, 768, 1024, 1536)
        for metric in ("cosine", "l2")
    }
)


def _include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """Filter objects considered by autogenerate and ``alembic check``."""
    if type_ == "table" and name == "alembic_version":
        return False
    if type_ == "index" and name in UNCOMPARABLE_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — used for reviewable DDL."""
    context.configure(
        url=settings.db.async_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # Postgres renders enum defaults as 'unknown'::doc_type while the model
        # declares the bare string, which autogenerate reports as a permanent
        # false difference. Server defaults are therefore reviewed by hand.
        compare_server_default=False,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # Postgres renders enum defaults as 'unknown'::doc_type while the model
        # declares the bare string, which autogenerate reports as a permanent
        # false difference. Server defaults are therefore reviewed by hand.
        compare_server_default=False,
        include_object=_include_object,
        # Deterministic constraint names in generated migrations.
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
