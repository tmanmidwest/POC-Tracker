"""Backup & restore service.

Produces a single downloadable archive of the whole instance — a *consistent*
SQLite snapshot plus the uploaded files (note attachments + screenshots) and the
persisted secret-key files — and restores from one.

Design notes:

* **Consistent DB snapshot.** We never raw-copy a live SQLite file (WAL mode
  would tear it). ``sqlite3.Connection.backup`` produces a clean single-file
  copy safe to take while the app is running.
* **Encryption.** When a passphrase is given the archive is a WinZip-AES-256
  ``.zip`` (via ``pyzipper``) — openable by standard tools with the passphrase.
  Without one it's a plain deflate zip. Archives contain secrets, so files are
  written ``0600``.
* **Restore applies on startup, not live.** Uploading an archive *stages* it
  (validate → decrypt/extract to a pending dir → drop a marker). The actual file
  swap happens once, early in the next startup (see ``apply_pending_restore``),
  before the DB engine opens — sidestepping any hot-swap-under-WAL hazard. A
  safety snapshot of the current state is taken first so a bad restore is
  reversible.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pyzipper
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import NoteAttachment, Project, ProjectNote, Screenshot
from app.models.backup_run import STATUS_FAILED, STATUS_SUCCESS, BackupRun
from app.services.migrations import is_known_revision

log = logging.getLogger(__name__)

# Bump if the archive layout changes incompatibly.
FORMAT_VERSION = 1

# Archive member layout.
_MANIFEST = "manifest.json"
_DB_MEMBER = "db/poct.db"
_KEYS_PREFIX = "keys/"
_ATTACH_PREFIX = "files/note_attachments/"
_SHOTS_PREFIX = "files/screenshots/"


class BackupError(Exception):
    """Raised for user-facing backup/restore problems (bad passphrase, corrupt
    archive, incompatible version)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _stamped_name(prefix: str) -> str:
    """A collision-resistant archive filename like ``poct-backup-…-ab12.zip``."""
    return f"{prefix}-{_now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.zip"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_db(dest: Path) -> None:
    """Write a consistent copy of the live SQLite DB to ``dest``."""
    settings = get_settings()
    src = sqlite3.connect(str(settings.database_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _read_db_revision(db_path: Path) -> str | None:
    """Read the applied Alembic revision out of a SQLite file."""
    try:
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _data_counts(db: Session) -> dict[str, int]:
    return {
        "projects": db.query(func.count(Project.id)).scalar() or 0,
        "notes": db.query(func.count(ProjectNote.id)).scalar() or 0,
        "attachments": db.query(func.count(NoteAttachment.id)).scalar() or 0,
        "screenshots": db.query(func.count(Screenshot.id)).scalar() or 0,
    }


def _open_zip_write(path: Path, passphrase: str | None) -> pyzipper.AESZipFile:
    z = pyzipper.AESZipFile(
        str(path), "w", compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES if passphrase else None,
    )
    if passphrase:
        z.setpassword(passphrase.encode("utf-8"))
    return z


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_backup(
    db: Session, *, created_by: str | None, passphrase: str | None = None
) -> BackupRun:
    """Generate a backup archive and record a :class:`BackupRun`.

    On failure a failed ``BackupRun`` is recorded and :class:`BackupError` (or
    the original exception) is raised.
    """
    settings = get_settings()
    settings.backups_dir.mkdir(parents=True, exist_ok=True)

    filename = _stamped_name("poct-backup")
    archive_path = settings.backups_dir / filename
    tmp_db = settings.backups_dir / f".snapshot-{secrets.token_hex(4)}.db"

    try:
        _snapshot_db(tmp_db)
        manifest = {
            "format_version": FORMAT_VERSION,
            "app_version": settings.app_version,
            "schema_revision": _read_db_revision(tmp_db),
            "created_at": _now().isoformat(),
            "created_by": created_by,
            "encrypted": bool(passphrase),
            "includes_secret_keys": True,
            "db_sha256": _sha256_file(tmp_db),
            "counts": _data_counts(db),
        }

        with _open_zip_write(archive_path, passphrase) as z:
            z.writestr(_MANIFEST, json.dumps(manifest, indent=2))
            z.write(str(tmp_db), _DB_MEMBER)
            for key_path in settings.secret_key_paths:
                if key_path.exists():
                    z.write(str(key_path), f"{_KEYS_PREFIX}{key_path.name}")
            _add_tree(z, settings.data_dir / "note_attachments", _ATTACH_PREFIX)
            _add_tree(z, settings.data_dir / "screenshots", _SHOTS_PREFIX)

        archive_path.chmod(0o600)  # contains secrets
        size = archive_path.stat().st_size

        run = BackupRun(
            filename=filename,
            size_bytes=size,
            encrypted=bool(passphrase),
            status=STATUS_SUCCESS,
            app_version=manifest["app_version"],
            schema_revision=manifest["schema_revision"],
            counts_json=json.dumps(manifest["counts"]),
            created_by=created_by,
        )
        db.add(run)
        db.commit()
        _prune_old(db)
        log.info("backup_created", extra={"archive": filename, "size_bytes": size})
        return run
    except Exception as exc:
        archive_path.unlink(missing_ok=True)
        run = BackupRun(
            status=STATUS_FAILED, encrypted=bool(passphrase),
            error=str(exc), created_by=created_by,
        )
        db.add(run)
        db.commit()
        log.exception("backup_failed")
        raise
    finally:
        tmp_db.unlink(missing_ok=True)


def _add_tree(z: pyzipper.AESZipFile, root: Path, arc_prefix: str) -> None:
    """Add every file under ``root`` to the zip under ``arc_prefix``."""
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            z.write(str(path), f"{arc_prefix}{path.relative_to(root).as_posix()}")


def _prune_old(db: Session) -> None:
    """Keep only the most recent N successful archives on disk."""
    settings = get_settings()
    keep = max(0, settings.backup_retention_count)
    successes = (
        db.query(BackupRun)
        .filter(BackupRun.status == STATUS_SUCCESS, BackupRun.filename.isnot(None))
        .order_by(BackupRun.created_at.desc(), BackupRun.id.desc())
        .all()
    )
    for run in successes[keep:]:
        delete_run(db, run)


# ---------------------------------------------------------------------------
# List / download / delete
# ---------------------------------------------------------------------------


def list_runs(db: Session) -> list[BackupRun]:
    return (
        db.query(BackupRun)
        .order_by(BackupRun.created_at.desc(), BackupRun.id.desc())
        .all()
    )


def archive_path(run: BackupRun) -> Path | None:
    if not run.filename:
        return None
    return get_settings().backups_dir / run.filename


def delete_run(db: Session, run: BackupRun) -> None:
    path = archive_path(run)
    if path is not None:
        path.unlink(missing_ok=True)
    db.delete(run)
    db.commit()


# ---------------------------------------------------------------------------
# Validate / stage restore
# ---------------------------------------------------------------------------


def validate_archive(path: Path, passphrase: str | None) -> dict:
    """Open and verify a backup archive, returning its manifest.

    Raises :class:`BackupError` with a user-facing message on any problem.
    """
    try:
        with pyzipper.AESZipFile(str(path)) as z:
            if passphrase:
                z.setpassword(passphrase.encode("utf-8"))
            names = set(z.namelist())
            if _MANIFEST not in names or _DB_MEMBER not in names:
                raise BackupError("This file is not a Questlog backup archive.")
            try:
                manifest = json.loads(z.read(_MANIFEST))
                db_bytes = z.read(_DB_MEMBER)
            except RuntimeError as exc:
                # pyzipper raises RuntimeError for a missing/incorrect password.
                raise BackupError(
                    "Incorrect passphrase, or this backup is encrypted and no "
                    "passphrase was provided."
                ) from exc
    except pyzipper.BadZipFile as exc:
        raise BackupError("The uploaded file is not a valid zip archive.") from exc

    if manifest.get("format_version", 0) > FORMAT_VERSION:
        raise BackupError(
            "This backup was created by a newer version of Questlog and "
            "cannot be restored here."
        )
    if _sha256_bytes(db_bytes) != manifest.get("db_sha256"):
        raise BackupError("Backup is corrupt: database checksum does not match.")
    if not is_known_revision(manifest.get("schema_revision")):
        raise BackupError(
            "This backup's database schema is newer than this app. Upgrade the "
            "app before restoring."
        )
    return manifest


def stage_restore(source: Path, passphrase: str | None) -> dict:
    """Validate an uploaded archive and extract it to the pending-restore area.

    The decrypted files are written to disk now (so applying at startup needs no
    passphrase), and a marker is dropped for ``apply_pending_restore``.
    """
    manifest = validate_archive(source, passphrase)
    settings = get_settings()

    staging = settings.restore_staging_dir
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    with pyzipper.AESZipFile(str(source)) as z:
        if passphrase:
            z.setpassword(passphrase.encode("utf-8"))
        z.extractall(str(staging))

    settings.restore_marker_path.write_text(
        json.dumps({"staged_at": _now().isoformat(), "manifest": manifest}, indent=2)
    )
    log.info("restore_staged", extra={"source": source.name})
    return manifest


def pending_restore_info() -> dict | None:
    """Return the staged-restore marker contents, or None if none pending."""
    marker = get_settings().restore_marker_path
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        return {"staged_at": None, "manifest": {}}


def cancel_pending_restore() -> bool:
    """Discard a staged restore without applying it. Returns True if one existed."""
    settings = get_settings()
    existed = settings.restore_marker_path.exists()
    settings.restore_marker_path.unlink(missing_ok=True)
    if settings.restore_staging_dir.exists():
        shutil.rmtree(settings.restore_staging_dir)
    return existed


# ---------------------------------------------------------------------------
# Apply restore (startup)
# ---------------------------------------------------------------------------


def apply_pending_restore() -> bool:
    """If a restore is staged, swap it into place. Call early at startup, before
    the DB engine opens. Returns True if a restore was applied.

    Takes a best-effort safety snapshot of the current state first, then replaces
    the DB, secret keys, and file directories from the staged copy. Schema is
    brought to head by the normal startup migration that runs afterwards.
    """
    settings = get_settings()
    if not settings.restore_marker_path.exists():
        return False

    staging = settings.restore_staging_dir
    log.info("restore_applying", extra={"staging": str(staging)})

    try:
        _safety_snapshot()
    except Exception:
        # Don't block an explicitly-requested restore on snapshot trouble, but
        # make the loss-of-rollback very visible.
        log.exception("restore_safety_snapshot_failed")

    data_dir = settings.data_dir

    # Database: drop WAL sidecars so the restored file isn't shadowed.
    staged_db = staging / _DB_MEMBER
    if staged_db.exists():
        for suffix in ("", "-wal", "-shm"):
            (data_dir / f"poct.db{suffix}").unlink(missing_ok=True)
        shutil.move(str(staged_db), str(settings.database_path))

    # Secret keys.
    staged_keys = staging / _KEYS_PREFIX.rstrip("/")
    if staged_keys.exists():
        for key_file in staged_keys.iterdir():
            shutil.move(str(key_file), str(data_dir / key_file.name))

    # File directories — replace wholesale so removed files don't linger.
    _replace_dir(staging / _ATTACH_PREFIX.rstrip("/"), data_dir / "note_attachments")
    _replace_dir(staging / _SHOTS_PREFIX.rstrip("/"), data_dir / "screenshots")

    shutil.rmtree(staging, ignore_errors=True)
    settings.restore_marker_path.unlink(missing_ok=True)
    log.info("restore_applied")
    return True


def _replace_dir(staged: Path, target: Path) -> None:
    """Replace ``target`` with ``staged`` (or an empty dir if staged is absent)."""
    if target.exists():
        shutil.rmtree(target)
    if staged.exists():
        shutil.move(str(staged), str(target))
    else:
        target.mkdir(parents=True, exist_ok=True)


def _safety_snapshot() -> None:
    """Archive the current on-disk state before a restore overwrites it.

    Runs at startup with nothing writing the DB, so a raw file copy is safe.
    """
    settings = get_settings()
    if not settings.database_path.exists():
        return  # nothing to snapshot (fresh instance)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    out = settings.backups_dir / _stamped_name("pre-restore")
    tmp_db = settings.backups_dir / f".snapshot-{secrets.token_hex(4)}.db"

    try:
        _snapshot_db(tmp_db)
        with _open_zip_write(out, None) as z:
            z.writestr(
                _MANIFEST,
                json.dumps(
                    {
                        "format_version": FORMAT_VERSION,
                        "app_version": settings.app_version,
                        "schema_revision": _read_db_revision(tmp_db),
                        "created_at": _now().isoformat(),
                        "created_by": "system (pre-restore)",
                        "encrypted": False,
                        "includes_secret_keys": True,
                        "db_sha256": _sha256_file(tmp_db),
                        "note": "Automatic safety snapshot taken before a restore.",
                    },
                    indent=2,
                ),
            )
            z.write(str(tmp_db), _DB_MEMBER)
            for key_path in settings.secret_key_paths:
                if key_path.exists():
                    z.write(str(key_path), f"{_KEYS_PREFIX}{key_path.name}")
            _add_tree(z, settings.data_dir / "note_attachments", _ATTACH_PREFIX)
            _add_tree(z, settings.data_dir / "screenshots", _SHOTS_PREFIX)
        out.chmod(0o600)
        log.info("restore_safety_snapshot_created", extra={"archive": out.name})
    finally:
        tmp_db.unlink(missing_ok=True)
