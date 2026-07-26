"""Cross-instance coordination via Postgres advisory locks.

When the app runs as more than one container behind the load balancer, work that
must happen *once* — schema migrations at startup, the daily audit/expiry sweeps
— would otherwise run in every instance: duplicate expiry emails, racing
migrations, wasted work.

A Postgres *advisory lock* is a cheap, app-defined mutex keyed by an integer.
:func:`advisory_lock` takes one so only the holder does the work.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator

from sqlalchemy import text

from app.db import get_engine

log = logging.getLogger(__name__)

# Arbitrary but stable keys — one per coordinated job. Keep them distinct.
LOCK_MIGRATIONS = 918_270_001
LOCK_AUDIT_RETENTION = 918_270_002
LOCK_EXTERNAL_EXPIRY = 918_270_003
LOCK_GOOGLE_SYNC = 918_270_004
LOCK_REPORT_SCHEDULER = 918_270_005


@contextlib.contextmanager
def advisory_lock(key: int, *, blocking: bool = True) -> Iterator[bool]:
    """Hold a Postgres advisory lock for the duration of the block.

    Yields ``True`` when the caller holds the lock and should do the work, or
    ``False`` when ``blocking=False`` and another instance already holds it.
    ``blocking=True`` waits until the lock is free (used to serialize startup
    migrations).
    """
    # AUTOCOMMIT so the session-level lock isn't wrapped in a long-lived
    # transaction; the lock lives on the connection until we unlock/close it.
    conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    held = False
    try:
        if blocking:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            held = True
        else:
            held = bool(
                conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
                ).scalar()
            )
        yield held
    finally:
        if held:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        conn.close()


def run_singleton(key: int, fn: Callable[[], None], *, label: str) -> None:
    """Run ``fn`` only if this instance can take ``key`` — else another does it.

    Non-blocking: if a sibling instance holds the lock, skip quietly (that
    instance is running the job).
    """
    with advisory_lock(key, blocking=False) as held:
        if not held:
            log.debug("singleton_skipped", extra={"job": label})
            return
        fn()
