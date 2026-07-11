"""CLI to load (or remove) demo data on a LOCAL testing instance.

For local demo/testing databases only — never run against production.

Usage:
    # Preview what would happen (dry run — writes nothing):
    docker exec -it poc-tracker python -m app.scripts.seed_demo

    # Actually load the demo dataset:
    docker exec -it poc-tracker python -m app.scripts.seed_demo --yes
    # or with the installed entry point:
    docker exec -it poc-tracker poct-seed-demo --yes

    # Remove the demo dataset again:
    docker exec -it poc-tracker poct-seed-demo --purge --yes

The command prints the target database before doing anything and requires an
explicit --yes to write, so you always see which instance you're about to
change. Seeding is idempotent (existing demo customers are skipped unless
--force). This is a guard against accidents, not a security boundary — don't
install the entry point where it could be run against real data.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.logging_config import configure_logging
from app.services.demo_data import (
    DEMO_CUSTOMER_NAMES,
    DEMO_USER_PASSWORD,
    purge_demo_data,
    seed_demo_data,
)


def _redacted_db_url() -> str:
    """The target database URL with any password masked."""
    url = get_engine().url
    try:
        return url.render_as_string(hide_password=True)
    except Exception:  # pragma: no cover - defensive
        return str(url)


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(
        description="Load or remove demo data on a LOCAL testing instance. "
        "Never run this against production.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually apply changes. Without it, prints a dry-run plan only.",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Remove the demo dataset instead of adding it.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="When seeding, re-create demo customers even if they already exist.",
    )
    args = parser.parse_args()

    action = "PURGE" if args.purge else "SEED"
    print(f"\nDemo data — {action}")
    print(f"  Target database: {_redacted_db_url()}")
    print(f"  Demo customers:  {', '.join(DEMO_CUSTOMER_NAMES)}")

    if not args.yes:
        print(
            "\n[DRY RUN] Nothing was changed. Re-run with --yes to apply.\n"
            "          Make sure the target database above is your demo/testing\n"
            "          instance and NOT production.\n"
        )
        return 0

    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        if args.purge:
            removed = purge_demo_data(db)
            print(
                f"\n[OK] Removed {removed['customers']} demo customer(s), "
                f"{removed['projects']} project(s), and "
                f"{removed['engineers']} demo engineer account(s).\n"
            )
            log.info("seed_demo_cli_purged", extra=removed)
            return 0

        try:
            summary = seed_demo_data(db, force=args.force)
        except RuntimeError as exc:
            print(f"\n[ERROR] {exc}\n", file=sys.stderr)
            log.error("seed_demo_cli_failed", extra={"error": str(exc)})
            return 1

    print(
        f"\n[OK] Created {summary['customers']} customer(s), "
        f"{summary['projects']} project(s), {summary['use_cases']} use case(s)."
    )
    if summary["skipped"]:
        print(
            f"     Skipped {summary['skipped']} customer(s) that already existed "
            f"(use --force to re-create)."
        )
    print(
        f"     Demo engineers 'amaya' and 'devlin' sign in with password "
        f"'{DEMO_USER_PASSWORD}'.\n"
    )
    log.info("seed_demo_cli_seeded", extra=summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
