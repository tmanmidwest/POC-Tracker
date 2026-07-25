"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Some test modules import `app.main`, which builds the app at import time and
# needs a writable data dir. Point at a temp dir before any such import so it
# doesn't fall back to the default "/data" (read-only on most machines). The
# autouse fixture below still gives each test its own isolated dir.
os.environ.setdefault(
    "POCT_DATA_DIR", tempfile.mkdtemp(prefix="poct-test-import-")
)

# The suite runs against Postgres (the only supported database). Use
# POCT_DATABASE_URL if set (CI points it at the service container), otherwise a
# local default — bring one up with `docker compose up -d postgres`. The schema
# is migrated once per session and each test starts from a truncated baseline.
_PG_TEST_URL = (
    os.environ.get("POCT_DATABASE_URL")
    or "postgresql+psycopg://poct:poct@localhost:5432/poct"
)
os.environ.setdefault("POCT_DATABASE_URL", _PG_TEST_URL)

# The post-migration row baseline (migration-seeded rows like the "Unassigned"
# region and the "Core" use-case library). We migrate once, snapshot these, and
# restore them after each per-test truncate so every test starts identically.
_PG_BASELINE: dict[str, list[dict]] = {}


def _pg_reset_and_migrate() -> None:
    """Drop + recreate the public schema, migrate to head, snapshot the baseline."""
    from sqlalchemy import create_engine, select, text

    from app.config import get_settings
    from app.db import Base
    from app.services.migrations import run_migrations

    eng = create_engine(_PG_TEST_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    eng.dispose()

    get_settings.cache_clear()
    import app.db as db_module

    db_module._engine = None  # type: ignore[attr-defined]
    db_module._SessionLocal = None  # type: ignore[attr-defined]
    run_migrations()

    # Capture the migration-seeded rows so each test can be reset back to them.
    import app.models  # noqa: F401
    _PG_BASELINE.clear()
    eng = create_engine(_PG_TEST_URL)
    with eng.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = [dict(m) for m in conn.execute(select(table)).mappings()]
            if rows:
                _PG_BASELINE[table.name] = rows
    eng.dispose()


def _pg_truncate_all() -> None:
    """Empty every table (keeping the migrated schema) so a test starts clean.

    Each test starts from an empty but migrated DB: ``client`` tests re-seed via
    app startup; ``db_session`` tests build their own data.
    """
    from sqlalchemy import create_engine, text

    import app.models  # noqa: F401  (register every table on Base.metadata)
    from app.db import Base

    sorted_tables = list(Base.metadata.sorted_tables)
    names = [f'"{t.name}"' for t in sorted_tables]
    names.append('"search_index"')  # raw-created in migration 0012, not an ORM table
    eng = create_engine(_PG_TEST_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        # A prior test may have left a session open ("idle in transaction"), which
        # holds locks and would make TRUNCATE block indefinitely. Terminate any
        # such leftover backends first (pool_pre_ping reconnects live pools).
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND state IS DISTINCT FROM 'active'"
            )
        )
        conn.execute(text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE"))

        # Restore the migration-seeded baseline (FK-safe order) so each test starts
        # exactly like a fresh SQLite migrate, then advance sequences past it.
        for table in sorted_tables:
            rows = _PG_BASELINE.get(table.name)
            if rows:
                conn.execute(table.insert(), rows)
        for table in sorted_tables:
            if table.name not in _PG_BASELINE:
                continue
            pk = list(table.primary_key.columns)
            if len(pk) != 1:
                continue
            col = pk[0].name
            seq = conn.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": table.name, "c": col},
            ).scalar()
            if seq:
                conn.execute(
                    text(
                        f'SELECT setval(:seq, (SELECT MAX("{col}") FROM "{table.name}"))'
                    ),
                    {"seq": seq},
                )
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _pg_session_schema() -> Iterator[None]:
    """Reset + migrate the schema once for the whole session."""
    _pg_reset_and_migrate()
    yield


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Give each test its own isolated data dir (and a clean database)."""
    with tempfile.TemporaryDirectory(prefix="poct-test-") as tmp:
        path = Path(tmp)
        monkeypatch.setenv("POCT_DATA_DIR", str(path))
        # Fresh files per test; empty the shared Postgres back to the migrated
        # baseline so this test starts from a clean slate.
        monkeypatch.setenv("POCT_DATABASE_URL", _PG_TEST_URL)
        _pg_truncate_all()
        # Clear the settings cache so fresh env vars are picked up
        from app.config import get_settings

        get_settings.cache_clear()
        # Reset module-level engine cache between tests
        import app.db as db_module

        db_module._engine = None  # type: ignore[attr-defined]
        db_module._SessionLocal = None  # type: ignore[attr-defined]
        # Reset cached system config so it reloads against each test's fresh DB
        import app.services.system_config as system_config_module

        system_config_module._cache = None  # type: ignore[attr-defined]
        yield path
        get_settings.cache_clear()
        # Dispose (not just drop) so pooled Postgres connections close and don't
        # linger into the next test's TRUNCATE.
        if db_module._engine is not None:  # type: ignore[attr-defined]
            db_module._engine.dispose()  # type: ignore[attr-defined]
        db_module._engine = None  # type: ignore[attr-defined]
        db_module._SessionLocal = None  # type: ignore[attr-defined]
        system_config_module._cache = None  # type: ignore[attr-defined]


@pytest.fixture
def client() -> Iterator[object]:
    """Return a FastAPI TestClient bound to a fresh app instance."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_session(client):  # type: ignore[no-untyped-def]
    """Log in as the seeded admin via session. Returns the same client."""
    from app.config import get_settings

    settings = get_settings()
    resp = client.post(
        "/api/v1/auth/session/login",
        json={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
    )
    assert resp.status_code == 200, resp.text
    return client


@pytest.fixture
def api_key(admin_session):  # type: ignore[no-untyped-def]
    """Create an API key and return its plaintext value."""
    resp = admin_session.post(
        "/api/v1/auth/api-keys/", json={"name": "Test Suite Key"}
    )
    assert resp.status_code == 201
    return str(resp.json()["key"])


@pytest.fixture
def auth_headers(api_key: str) -> dict[str, str]:
    """Authorization header dict for REST API calls."""
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
def api_client(client, auth_headers):  # type: ignore[no-untyped-def]
    """A TestClient with API key default headers."""
    client.headers.update(auth_headers)
    return client
