"""Tests for the backup & restore service.

Cover the archive round-trip, passphrase encryption, validation rejections,
retention pruning, and the stage→apply restore flow (including the automatic
pre-restore safety snapshot).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import pyzipper
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.backup_run import STATUS_SUCCESS, BackupRun
from app.services import backups


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Migrate the isolated test DB and yield a session."""
    from app.db import get_session_factory
    from app.services.migrations import run_migrations

    run_migrations()
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def _rebuild_engine() -> None:
    """Drop the cached engine so the next session opens the (possibly swapped) DB."""
    import app.db as db_module

    if db_module._engine is not None:  # type: ignore[attr-defined]
        db_module._engine.dispose()  # type: ignore[attr-defined]
    db_module._engine = None  # type: ignore[attr-defined]
    db_module._SessionLocal = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Create + validate
# ---------------------------------------------------------------------------


def test_backup_route_failure_is_recorded(
    client: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed backup from the UI lands in the activity log, not just a flash."""
    import json

    settings = get_settings()
    resp = client.post(  # type: ignore[attr-defined]
        "/ui/login",
        data={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(backups, "create_backup", boom)

    resp = client.post("/ui/settings/backups/create", follow_redirects=False)  # type: ignore[attr-defined]
    assert resp.status_code == 303  # redirects back, no crash

    events = json.loads(
        client.get("/ui/activity/export.json?category=system").text  # type: ignore[attr-defined]
    )
    failures = [e for e in events if e["event_type"] == "backup.failed"]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failure"
    assert "disk full" in failures[0]["detail"]["error"]


def test_create_backup_roundtrip(db_session: Session) -> None:
    run = backups.create_backup(db_session, created_by="tester")
    assert run.status == STATUS_SUCCESS
    assert run.encrypted is False
    path = backups.archive_path(run)
    assert path is not None and path.exists()

    manifest = backups.validate_archive(path, None)
    assert manifest["app_version"]
    assert manifest["schema_revision"]
    assert set(manifest["counts"]) == {"projects", "notes", "attachments", "screenshots"}


def test_encrypted_backup_requires_passphrase(db_session: Session) -> None:
    run = backups.create_backup(db_session, created_by="tester", passphrase="hunter2")
    assert run.encrypted is True
    path = backups.archive_path(run)
    assert path is not None

    # Correct passphrase validates.
    assert backups.validate_archive(path, "hunter2")["encrypted"] is True
    # Wrong passphrase is rejected.
    with pytest.raises(backups.BackupError):
        backups.validate_archive(path, "wrong")
    # Missing passphrase on an encrypted archive is rejected.
    with pytest.raises(backups.BackupError):
        backups.validate_archive(path, None)


def test_validate_rejects_newer_format(db_session: Session) -> None:
    import json

    settings = get_settings()
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    bad = settings.backups_dir / "fake.zip"
    with pyzipper.AESZipFile(str(bad), "w") as z:
        z.writestr("db/poct.db", b"x")
        z.writestr(
            "manifest.json",
            json.dumps({"format_version": 999, "db_sha256": "irrelevant"}),
        )
    with pytest.raises(backups.BackupError, match="newer version"):
        backups.validate_archive(bad, None)


def test_validate_rejects_unknown_schema(db_session: Session) -> None:
    import hashlib
    import json

    settings = get_settings()
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    bad = settings.backups_dir / "badschema.zip"
    db_bytes = b"not-a-real-db"
    with pyzipper.AESZipFile(str(bad), "w") as z:
        z.writestr("db/poct.db", db_bytes)
        z.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format_version": 1,
                    "db_sha256": hashlib.sha256(db_bytes).hexdigest(),
                    "schema_revision": "zzz_not_a_real_revision",
                }
            ),
        )
    with pytest.raises(backups.BackupError, match="schema is newer"):
        backups.validate_archive(bad, None)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_keeps_only_latest(db_session: Session) -> None:
    # Default retention is 2.
    for _ in range(3):
        backups.create_backup(db_session, created_by="tester")
    successes = (
        db_session.query(BackupRun)
        .filter(BackupRun.status == STATUS_SUCCESS)
        .all()
    )
    assert len(successes) == 2
    # And only the kept archives remain on disk.
    files = list(get_settings().backups_dir.glob("poct-backup-*.zip"))
    assert len(files) == 2


# ---------------------------------------------------------------------------
# Stage + apply restore
# ---------------------------------------------------------------------------


def test_stage_and_apply_restore_reverts_data(db_session: Session) -> None:
    settings = get_settings()

    # State A: take a backup of the current DB. The DB snapshot is taken before
    # the run row is committed, so backup A contains zero backup_runs rows.
    run = backups.create_backup(db_session, created_by="tester")
    archive = backups.archive_path(run)
    assert archive is not None

    # Mutate: add a sentinel row that does NOT exist in backup A.
    db_session.add(BackupRun(status=STATUS_SUCCESS, filename="SENTINEL", encrypted=False))
    db_session.commit()
    assert db_session.query(BackupRun).filter_by(filename="SENTINEL").count() == 1

    # Stage the restore of A.
    backups.stage_restore(archive, None)
    assert settings.restore_marker_path.exists()
    assert backups.pending_restore_info() is not None

    # Simulate startup: release the engine, then apply.
    db_session.close()
    _rebuild_engine()
    applied = backups.apply_pending_restore()
    assert applied is True
    assert not settings.restore_marker_path.exists()

    # A pre-restore safety snapshot should have been written.
    assert list(settings.backups_dir.glob("pre-restore-*.zip"))

    # Reopen and confirm the sentinel is gone (state reverted to A).
    from app.db import get_session_factory

    _rebuild_engine()
    fresh = get_session_factory()()
    try:
        assert fresh.query(BackupRun).filter_by(filename="SENTINEL").count() == 0
        # Snapshot A predates its own run row, so it holds no backup_runs.
        assert fresh.query(BackupRun).count() == 0
    finally:
        fresh.close()


def test_apply_is_noop_without_marker(db_session: Session) -> None:
    assert backups.apply_pending_restore() is False


def test_cancel_pending_restore(db_session: Session) -> None:
    run = backups.create_backup(db_session, created_by="tester")
    archive = backups.archive_path(run)
    assert archive is not None
    backups.stage_restore(archive, None)
    assert backups.pending_restore_info() is not None

    assert backups.cancel_pending_restore() is True
    assert backups.pending_restore_info() is None
    assert not get_settings().restore_staging_dir.exists()
