"""CLI script to reset the seeded admin's password to the configured default.

Usage:
    docker exec -it poc-tracker python -m app.scripts.reset_admin_password
    # or with the installed entry point:
    docker exec -it poc-tracker poct-reset-admin

Only affects the user with is_seeded=True (i.e., the bootstrapped robbytheadmin).
Other admin users are left untouched.
"""

from __future__ import annotations

import logging
import sys

from app.config import get_settings
from app.db import get_session_factory
from app.logging_config import configure_logging
from app.services.seed_data import reset_admin_password


def main() -> int:
    """Run the reset. Returns process exit code."""
    settings = get_settings()
    configure_logging(settings.log_level)

    log = logging.getLogger(__name__)

    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        success = reset_admin_password(db, settings)

    if success:
        print(
            f"\n[OK] Password for '{settings.initial_admin_username}' "
            f"has been reset to the configured default.\n"
            f"     Other admin accounts are unaffected.\n"
        )
        log.info("reset_admin_cli_success")
        return 0

    print(
        f"\n[ERROR] Could not find a seeded admin user named "
        f"'{settings.initial_admin_username}'. Has the database been "
        f"initialized? Try starting the app once first.\n",
        file=sys.stderr,
    )
    log.error("reset_admin_cli_user_not_found")
    return 1


if __name__ == "__main__":
    sys.exit(main())
