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
        z.writestr(
            "manifest.json",
            json.dumps({"format_version": 999}),
        )
    with pytest.raises(backups.BackupError, match="newer version"):
        backups.validate_archive(bad, None)


def test_validate_rejects_non_archive(db_session: Session) -> None:
    """A zip without a manifest isn't a Questlog backup."""
    settings = get_settings()
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    bad = settings.backups_dir / "random.zip"
    with pyzipper.AESZipFile(str(bad), "w") as z:
        z.writestr("something.txt", b"not a backup")
    with pytest.raises(backups.BackupError, match="not a Questlog backup"):
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


def test_stage_and_apply_restore_reverts_files(db_session: Session) -> None:
    """Applying a restore reverts the uploaded-files directory to the archive's
    state and writes a pre-restore safety snapshot. (The database is restored
    out-of-band via the managed DB, so it isn't part of this flow.)"""
    settings = get_settings()
    attachments = settings.data_dir / "note_attachments"
    attachments.mkdir(parents=True, exist_ok=True)

    # State A: a file that exists at backup time.
    sentinel = attachments / "brief.txt"
    sentinel.write_text("original")
    run = backups.create_backup(db_session, created_by="tester")
    archive = backups.archive_path(run)
    assert archive is not None

    # Mutate the files after the backup: change one, add another.
    sentinel.write_text("CHANGED")
    (attachments / "extra.txt").write_text("added later")

    # Stage + apply the restore of A.
    backups.stage_restore(archive, None)
    assert settings.restore_marker_path.exists()
    assert backups.pending_restore_info() is not None

    db_session.close()
    _rebuild_engine()
    applied = backups.apply_pending_restore()
    assert applied is True
    assert not settings.restore_marker_path.exists()

    # A pre-restore safety snapshot should have been written.
    assert list(settings.backups_dir.glob("pre-restore-*.zip"))

    # Files reverted to state A: original content back, later addition gone.
    assert sentinel.read_text() == "original"
    assert not (attachments / "extra.txt").exists()


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
