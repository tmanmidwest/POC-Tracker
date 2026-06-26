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


def head_revision() -> str | None:
    """Return the latest migration revision known to this codebase (the head)."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_build_alembic_config())
    return script.get_current_head()


def is_known_revision(revision: str | None) -> bool:
    """Whether the given revision exists in this codebase's migration history.

    Used to reject restoring a backup taken from a *newer* app version, whose
    schema revision this code wouldn't know how to handle.
    """
    if not revision:
        # No revision recorded — treat as restorable (migrations will bring it up).
        return True
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_build_alembic_config())
    try:
        return script.get_revision(revision) is not None
    except Exception:
        return False
