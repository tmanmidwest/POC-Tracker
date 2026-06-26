"""Backup run model — history of backup archives generated from the UI.

Each row records one attempt to produce a downloadable backup archive (a DB
snapshot plus uploaded files). The archive itself lives on disk under
``<data_dir>/backups``; this table is the user-facing history/listing and is
what the download/delete routes look up.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


class BackupRun(Base, TimestampMixin):
    """A generated backup archive (or a failed attempt to generate one)."""

    __tablename__ = "backup_runs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # On-disk filename of the archive under <data_dir>/backups (null if failed).
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_SUCCESS)
    # Populated on failure for display in the history table.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance captured from the manifest, for display and restore checks.
    app_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    schema_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # JSON blob of row/file counts (projects, notes, attachments, screenshots).
    counts_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Username of whoever triggered the backup.
    created_by: Mapped[str | None] = mapped_column(String(150), nullable=True)

    def __repr__(self) -> str:
        return f"<BackupRun id={self.id} status={self.status} file={self.filename}>"
