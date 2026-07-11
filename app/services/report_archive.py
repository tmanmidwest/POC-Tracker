"""Build a single .zip bundling everything captured for a project: the
generated report PDF, every use-case screenshot, and every journal attachment.

Files are read straight from disk and laid out in readable folders so the
archive is useful for assembling readouts and slide decks afterwards.
"""

from __future__ import annotations

import io
import re
import zipfile

from app.models import Project, ProjectNote
from app.services import note_attachments as note_store
from app.services import screenshots as screenshot_store

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str | None, *, fallback: str = "file") -> str:
    """Collapse anything non-filename-safe into single dashes."""
    cleaned = _UNSAFE.sub("-", (name or "").strip()).strip("-")
    return cleaned or fallback


def project_has_artifacts(project: Project, notes: list[ProjectNote]) -> bool:
    """True if the project has any screenshots or journal attachments to bundle.

    ``notes`` is the caller-filtered list of notes to consider (see
    ``visible_project_notes``) so internal-only attachments are excluded for
    external viewers.
    """
    if any(uc.screenshots for uc in project.use_cases):
        return True
    return any(note.attachments for note in notes)


def build_project_archive(
    project: Project, pdf_bytes: bytes | None, notes: list[ProjectNote]
) -> bytes:
    """Return the bytes of a .zip containing the report PDF (if provided),
    all screenshots, and all journal attachments.

    ``notes`` is the caller-filtered list of notes whose attachments to bundle
    (see ``visible_project_notes``), keeping internal-only attachments out of an
    external viewer's export."""
    slug = _safe(project.display_name, fallback=f"project-{project.id}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if pdf_bytes:
            zf.writestr(f"{slug}/report.pdf", pdf_bytes)

        for uc in project.use_cases:
            uc_label = _safe(uc.reference_number or uc.name)[:50]
            for i, shot in enumerate(uc.screenshots, 1):
                path = screenshot_store.path_for(shot)
                if not path.exists():
                    continue
                name = _safe(shot.original_filename or shot.stored_filename)
                zf.write(path, f"{slug}/screenshots/{uc_label}/{i:02d}-{shot.id}-{name}")

        for note in notes:
            for att in note.attachments:
                path = note_store.path_for(att)
                if not path.exists():
                    continue
                name = _safe(att.original_filename or att.stored_filename)
                zf.write(
                    path,
                    f"{slug}/attachments/{note.note_date.isoformat()}-{att.id}-{name}",
                )

    return buf.getvalue()
