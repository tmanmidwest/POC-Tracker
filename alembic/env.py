"""Alembic environment configuration.

Overrides the static sqlalchemy.url from alembic.ini with the runtime database URL
from app settings, so migrations target the same DB the app uses.

We deliberately skip fileConfig() here because it would replace the app's
JSON logging configuration (configured in app.logging_config) with the plain-text
formatter from alembic.ini, causing seed_data logs after migrations to silently
disappear into a re-configured root logger.
"""

from __future__ import annotations

from sqlalchemy import engine_from_config, pool

# Import the models package so Alembic sees all tables in Base.metadata.
import app.models  # noqa: F401
from alembic import context
from app.config import get_settings
from app.db import Base

config = context.config

# Override the URL from the application's settings
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite-friendly ALTER TABLE
    )
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
