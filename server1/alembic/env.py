"""Alembic environment configuration for the database infrastructure."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.infrastructure.database.session import to_sync_url
# Import all ORM models so their tables are registered on the shared metadata.
from app.models import (
    ai_identity,
    app_record,
    brain_memory,
    brain_processing,
    device,
    installed_module,
    knowledge_graph,
    memory_vault,
    module_permission,
    personal_ai_brain,
    session,
    timeline,
    user,
)
from app.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
if settings.postgres_url:
    # Use the installed psycopg v3 driver explicitly; a bare ``postgresql://``
    # URL would otherwise resolve to psycopg2, which is not a dependency.
    config.set_main_option("sqlalchemy.url", to_sync_url(settings.postgres_url))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
