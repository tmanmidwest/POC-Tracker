"""Backup & restore service.

Produces a single downloadable archive of the instance's **database data**, its
**uploaded files** (note attachments + screenshots) and the persisted
**secret-key** files, and restores from one. A restored archive can seed a fresh,
empty instance — e.g. pulling ``questlog-tst`` (RDS) down into a local Docker
Postgres for development.

Design notes:

* **Database.** Every ORM table is dumped to portable JSON (see ``_dump_database``)
  rather than a ``pg_dump`` binary blob, so it restores across environments and
  Postgres versions without external tooling. Values that JSON can't represent
  natively (datetime, Decimal, bytes, UUID) are type-tagged; ``JSON``/``JSONB``
  columns pass through untouched. This is Postgres-oriented: restore truncates and
  reloads within one transaction and resets serial sequences, mirroring
  ``app/scripts/migrate_sqlite_to_postgres.py``. Managed snapshots (RDS + PITR)
  remain the disaster-recovery path; this feature is for portable, whole-instance
  copies.
* **Encryption.** When a passphrase is given the archive is a WinZip-AES-256
  ``.zip`` (via ``pyzipper``) — openable by standard tools with the passphrase.
  Without one it's a plain deflate zip. Archives contain secrets and data, so
  files are written ``0600``.
* **Restore applies on startup, in two phases.** Uploading an archive *stages* it
  (validate → decrypt/extract to a pending dir → drop a marker). Early on the next
  startup ``apply_pending_restore`` swaps the files + keys in and relocates the
  database payload aside; then, *after* migrations bring the schema to head,
  ``apply_pending_db_restore`` loads the data. A safety snapshot of the current
  files/keys is taken first so a bad file restore is reversible.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import shutil
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pyzipper
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.sql.sqltypes import JSON as SA_JSON

# Importing the models package registers every table on Base.metadata.
import app.models  # noqa: F401
from app.config import get_settings
from app.db import Base, get_engine
from app.models import NoteAttachment, Project, ProjectNote, Screenshot
from app.models.backup_run import STATUS_FAILED, STATUS_SUCCESS, BackupRun

log = logging.getLogger(__name__)

# Bump if the archive layout changes incompatibly. v3 adds the database payload;
# v2 archives (files + keys only) still restore — their DB load is simply skipped.
FORMAT_VERSION = 3

# Archive member layout.
_MANIFEST = "manifest.json"
_KEYS_PREFIX = "keys/"
_ATTACH_PREFIX = "files/note_attachments/"
_SHOTS_PREFIX = "files/screenshots/"
_DB_PREFIX = "database/"
_DB_MANIFEST = "database/manifest.json"
_DB_TABLES_PREFIX = "database/tables/"

# Not an ORM model (created by raw SQL in migration 0012); truncated with the
# rest and repopulated by its per-row triggers as rows are inserted on restore.
_SEARCH_INDEX = "search_index"


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


def _live_db_revision(db: Session) -> str | None:
    """Read the applied Alembic revision from the live DB (recorded for info)."""
    try:
        row = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        return row[0] if row else None
    except Exception:  # pragma: no cover - defensive
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


def _add_tree(z: pyzipper.AESZipFile, root: Path, arc_prefix: str) -> None:
    """Add every file under ``root`` to the zip under ``arc_prefix``."""
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            z.write(str(path), f"{arc_prefix}{path.relative_to(root).as_posix()}")


# ---------------------------------------------------------------------------
# Database dump / load (portable, type-tagged JSON)
# ---------------------------------------------------------------------------
#
# Values are stored as JSON. Types JSON can't represent natively are wrapped as
# ``{"__t": <tag>, "v": <payload>}``; ``JSON``/``JSONB`` columns pass through
# untouched (decode is driven by the column type, so a tagged-looking dict inside
# a JSON column is never misread). Symmetric encode/decode keeps a round-trip
# lossless across Postgres versions and environments.


def _is_json_col(column) -> bool:
    """True for ``JSON``/``JSONB`` columns, whose values are opaque JSON."""
    return isinstance(column.type, SA_JSON)


def _encode_value(value: object) -> object:
    """Make one scalar column value JSON-safe (see module note above)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):  # subclass of date — check first
        return {"__t": "dt", "v": value.isoformat()}
    if isinstance(value, date):
        return {"__t": "d", "v": value.isoformat()}
    if isinstance(value, time):
        return {"__t": "tm", "v": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__t": "dec", "v": str(value)}
    if isinstance(value, uuid.UUID):
        return {"__t": "uuid", "v": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__t": "b64", "v": base64.b64encode(bytes(value)).decode("ascii")}
    return value  # lists (ARRAY), etc. — already JSON-serialisable


def _decode_value(value: object) -> object:
    """Inverse of :func:`_encode_value` for a non-JSON column value."""
    if isinstance(value, dict) and "__t" in value:
        tag, payload = value["__t"], value["v"]
        if tag == "dt":
            return datetime.fromisoformat(payload)
        if tag == "d":
            return date.fromisoformat(payload)
        if tag == "tm":
            return time.fromisoformat(payload)
        if tag == "dec":
            return Decimal(payload)
        if tag == "uuid":
            return uuid.UUID(payload)
        if tag == "b64":
            return base64.b64decode(payload)
    return value


def _db_tables() -> list:
    """Every ORM table in FK-dependency order (parents first)."""
    return list(Base.metadata.sorted_tables)


def _dump_database(z: pyzipper.AESZipFile, db: Session) -> dict[str, int]:
    """Write every ORM table to the archive as type-tagged JSON. Returns row
    counts per table (informational, also stored in the DB manifest)."""
    counts: dict[str, int] = {}
    bind = db.get_bind()
    for table in _db_tables():
        json_cols = {c.name for c in table.columns if _is_json_col(c)}
        rows = []
        for m in db.execute(select(table)).mappings():
            rows.append(
                {
                    k: (v if k in json_cols else _encode_value(v))
                    for k, v in m.items()
                }
            )
        counts[table.name] = len(rows)
        z.writestr(
            f"{_DB_TABLES_PREFIX}{table.name}.json",
            json.dumps(rows, separators=(",", ":")),
        )

    manifest = {
        "schema_revision": _live_db_revision(db),
        "backend": make_url(str(bind.engine.url)).get_backend_name(),
        "tables": [t.name for t in _db_tables()],
        "counts": counts,
    }
    z.writestr(_DB_MANIFEST, json.dumps(manifest, indent=2))
    return counts


def _reset_sequence(conn, table) -> None:
    """Reset a Postgres serial sequence to the table's current max id."""
    pk_cols = list(table.primary_key.columns)
    if len(pk_cols) != 1:
        return  # composite PKs aren't serial
    col = pk_cols[0].name
    seq = conn.execute(
        text("SELECT pg_get_serial_sequence(:t, :c)"),
        {"t": table.name, "c": col},
    ).scalar()
    if not seq:
        return  # no sequence (fixed-id singleton, string PK, etc.)
    conn.execute(
        text(
            f'SELECT setval(:seq, COALESCE((SELECT MAX("{col}") FROM "{table.name}"), 1), '
            f'(SELECT MAX("{col}") FROM "{table.name}") IS NOT NULL)'
        ),
        {"seq": seq},
    )


def _load_database(engine: Engine, db_staging: Path) -> dict[str, int]:
    """Truncate every table and reload it from a staged database dump, in one
    transaction; then reset serial sequences. Postgres-specific. Returns counts."""
    tables = _db_tables()
    tables_dir = db_staging / "tables"
    counts: dict[str, int] = {}

    with engine.begin() as conn:
        # Clear everything (incl. seeded rows and the search index) so the load is
        # an exact copy. CASCADE handles FK order; the search-index triggers
        # repopulate as rows are inserted below.
        all_names = ", ".join(f'"{t.name}"' for t in tables) + f', "{_SEARCH_INDEX}"'
        conn.execute(text(f"TRUNCATE {all_names} RESTART IDENTITY CASCADE"))

        for table in tables:
            member = tables_dir / f"{table.name}.json"
            if not member.exists():
                counts[table.name] = 0
                continue
            raw_rows = json.loads(member.read_text())
            json_cols = {c.name for c in table.columns if _is_json_col(c)}
            rows = [
                {
                    k: (v if k in json_cols else _decode_value(v))
                    for k, v in row.items()
                }
                for row in raw_rows
            ]
            if rows:
                conn.execute(table.insert(), rows)
            counts[table.name] = len(rows)

        for table in tables:
            _reset_sequence(conn, table)

    return counts


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_backup(
    db: Session, *, created_by: str | None, passphrase: str | None = None
) -> BackupRun:
    """Generate a files + keys backup archive and record a :class:`BackupRun`.

    On failure a failed ``BackupRun`` is recorded and :class:`BackupError` (or
    the original exception) is raised.
    """
    settings = get_settings()
    settings.backups_dir.mkdir(parents=True, exist_ok=True)

    filename = _stamped_name("poct-backup")
    archive_path = settings.backups_dir / filename

    try:
        manifest = {
            "format_version": FORMAT_VERSION,
            "app_version": settings.app_version,
            "schema_revision": _live_db_revision(db),
            "created_at": _now().isoformat(),
            "created_by": created_by,
            "encrypted": bool(passphrase),
            "includes_secret_keys": True,
            "includes_database": True,
            "counts": _data_counts(db),
        }

        with _open_zip_write(archive_path, passphrase) as z:
            z.writestr(_MANIFEST, json.dumps(manifest, indent=2))
            for key_path in settings.secret_key_paths:
                if key_path.exists():
                    z.write(str(key_path), f"{_KEYS_PREFIX}{key_path.name}")
            _add_tree(z, settings.data_dir / "note_attachments", _ATTACH_PREFIX)
            _add_tree(z, settings.data_dir / "screenshots", _SHOTS_PREFIX)
            _dump_database(z, db)

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

    Raises :class:`BackupError` with a user-facing message on any problem. Older
    (v2) archives without a database member are accepted — they restore files +
    keys only, and the database load step is simply skipped.
    """
    try:
        with pyzipper.AESZipFile(str(path)) as z:
            if passphrase:
                z.setpassword(passphrase.encode("utf-8"))
            names = set(z.namelist())
            if _MANIFEST not in names:
                raise BackupError("This file is not a Questlog backup archive.")
            try:
                manifest = json.loads(z.read(_MANIFEST))
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
    existed = (
        settings.restore_marker_path.exists()
        or settings.restore_db_marker_path.exists()
    )
    settings.restore_marker_path.unlink(missing_ok=True)
    settings.restore_db_marker_path.unlink(missing_ok=True)
    if settings.restore_staging_dir.exists():
        shutil.rmtree(settings.restore_staging_dir)
    if settings.restore_db_staging_dir.exists():
        shutil.rmtree(settings.restore_db_staging_dir)
    return existed


# ---------------------------------------------------------------------------
# Apply restore (startup)
# ---------------------------------------------------------------------------


def apply_pending_restore() -> bool:
    """Phase 1 of restore: swap the files + keys into place. Call early at
    startup, BEFORE migrations. Returns True if a file restore was applied.

    Takes a best-effort safety snapshot of the current files/keys first, then
    replaces the secret keys and uploaded-file directories from the staged copy.
    A database payload (v3 archives) is relocated to its own pending area and a
    marker dropped so :func:`apply_pending_db_restore` can load it *after*
    migrations bring the schema up to head. v2 archives have no such payload.
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

    # Secret keys.
    staged_keys = staging / _KEYS_PREFIX.rstrip("/")
    if staged_keys.exists():
        for key_file in staged_keys.iterdir():
            shutil.move(str(key_file), str(data_dir / key_file.name))

    # File directories — replace wholesale so removed files don't linger.
    _replace_dir(staging / _ATTACH_PREFIX.rstrip("/"), data_dir / "note_attachments")
    _replace_dir(staging / _SHOTS_PREFIX.rstrip("/"), data_dir / "screenshots")

    # Relocate any database payload so it survives this staging cleanup and can be
    # loaded once the schema exists (post-migration).
    staged_db = staging / _DB_PREFIX.rstrip("/")
    if (staged_db / "manifest.json").exists():
        db_pending = settings.restore_db_staging_dir
        if db_pending.exists():
            shutil.rmtree(db_pending)
        shutil.move(str(staged_db), str(db_pending))
        settings.restore_db_marker_path.write_text(
            json.dumps({"staged_at": _now().isoformat()}, indent=2)
        )
        log.info("restore_db_payload_staged")

    shutil.rmtree(staging, ignore_errors=True)
    settings.restore_marker_path.unlink(missing_ok=True)
    log.info("restore_applied")
    return True


def apply_pending_db_restore() -> bool:
    """Phase 2 of restore: load a staged database payload. Call at startup AFTER
    migrations have brought the schema to head. Returns True if data was loaded.

    Postgres-only (truncate + reload + sequence reset). If the archive's schema
    revision doesn't match the live one, the load is skipped (and the payload
    discarded) so the app still boots rather than crash-looping — realign the two
    instances on the same app version and restore again.
    """
    settings = get_settings()
    if not settings.restore_db_marker_path.exists():
        return False

    db_staging = settings.restore_db_staging_dir

    def _cleanup() -> None:
        shutil.rmtree(db_staging, ignore_errors=True)
        settings.restore_db_marker_path.unlink(missing_ok=True)

    try:
        engine = get_engine()
        if make_url(str(engine.url)).get_backend_name() != "postgresql":
            log.error(
                "restore_db_skipped_not_postgres",
                extra={"backend": make_url(str(engine.url)).get_backend_name()},
            )
            _cleanup()
            return False

        db_manifest = json.loads((db_staging / "manifest.json").read_text())
        archive_rev = db_manifest.get("schema_revision")
        with engine.connect() as c:
            live_rev = c.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            ).scalar()
        if archive_rev != live_rev:
            log.error(
                "restore_db_skipped_revision_mismatch",
                extra={"archive_revision": archive_rev, "live_revision": live_rev},
            )
            _cleanup()
            return False

        log.info("restore_db_loading")
        counts = _load_database(engine, db_staging)
        total = sum(counts.values())
        _cleanup()
        log.info("restore_db_loaded", extra={"rows": total, "tables": len(counts)})
        return True
    except Exception:
        # Discard the payload so a persistent failure can't wedge every startup;
        # the original archive still lives on the operator's machine to retry.
        log.exception("restore_db_failed")
        _cleanup()
        return False


def _replace_dir(staged: Path, target: Path) -> None:
    """Replace ``target`` with ``staged`` (or an empty dir if staged is absent)."""
    if target.exists():
        shutil.rmtree(target)
    if staged.exists():
        shutil.move(str(staged), str(target))
    else:
        target.mkdir(parents=True, exist_ok=True)


def _safety_snapshot() -> None:
    """Archive the current files + keys before a restore overwrites them."""
    settings = get_settings()
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    out = settings.backups_dir / _stamped_name("pre-restore")

    with _open_zip_write(out, None) as z:
        z.writestr(
            _MANIFEST,
            json.dumps(
                {
                    "format_version": FORMAT_VERSION,
                    "app_version": settings.app_version,
                    "created_at": _now().isoformat(),
                    "created_by": "system (pre-restore)",
                    "encrypted": False,
                    "includes_secret_keys": True,
                    "note": "Automatic safety snapshot taken before a restore.",
                },
                indent=2,
            ),
        )
        for key_path in settings.secret_key_paths:
            if key_path.exists():
                z.write(str(key_path), f"{_KEYS_PREFIX}{key_path.name}")
        _add_tree(z, settings.data_dir / "note_attachments", _ATTACH_PREFIX)
        _add_tree(z, settings.data_dir / "screenshots", _SHOTS_PREFIX)
    out.chmod(0o600)
    log.info("restore_safety_snapshot_created", extra={"archive": out.name})
