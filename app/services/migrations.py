"""Run Alembic migrations programmatically at app startup.

This is preferable to a separate entrypoint script because it guarantees the
database schema is always in sync with the code version actually running.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.config import get_settings

log = logging.getLogger(__name__)


def _build_alembic_config() -> Config:
    """Build an Alembic Config pointing at the project's alembic.ini."""
    settings = get_settings()
    # alembic.ini lives at the repo root, alongside the app/ package
    repo_root = Path(__file__).resolve().parents[2]
    ini_path = repo_root / "alembic.ini"
    if not ini_path.exists():
        raise FileNotFoundError(
            f"Could not find alembic.ini at {ini_path}. "
            "Ensure the package is installed correctly."
        )
    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def run_migrations() -> None:
    """Bring the database up to the latest migration head."""
    settings = get_settings()
    settings.ensure_data_dir()
    cfg = _build_alembic_config()
    log.info("running_migrations", extra={"database_url": settings.database_url})
    command.upgrade(cfg, "head")
    log.info("migrations_complete")
