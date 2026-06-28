"""Spreadsheet export/import of a project's use cases (deterministic, no AI).

One column layout is shared by export, the blank template, and import. The
optional ``id`` column drives upsert: a row whose ``id`` matches an existing use
case in the project updates it; a blank ``id`` inserts a new one. Status and
feature type are matched by name (case-insensitive) so the sheet stays readable.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy.orm import Session

from app.models import (
    FeatureType,
    Project,
    ProjectUseCase,
    UseCaseLibrary,
    UseCaseStatus,
)

log = logging.getLogger(__name__)

# Display headers (row 1). Parsing normalizes these (lower + spaces→underscores)
# so "Reference Number" maps to the field key "reference_number".
HEADERS = [
    "Id", "Reference Number", "Category", "Name", "Description",
    "Success Validation", "Feature Type", "Status", "Comments", "Completed On",
]
# Field keys in the same order, used for parsing and round-tripping.
KEYS = [
    "id", "reference_number", "category", "name", "description",
    "success_validation", "feature_type", "status", "comments", "completed_on",
]
MAX_ROWS = 2000


# --- Library layout (master template list) -------------------------------
# The library has no status/comments/completed-on; it does carry an Active flag.
LIBRARY_HEADERS = [
    "Id", "Reference Number", "Category", "Name", "Description",
    "Success Validation", "Feature Type", "Active",
]
LIBRARY_KEYS = [
    "id", "reference_number", "category", "name", "description",
    "success_validation", "feature_type", "active",
]


# ---------------------------------------------------------------------------
# Export & template
# ---------------------------------------------------------------------------


def build_export_xlsx(project: Project) -> bytes:
    """Export the project's current use cases as an .xlsx (with ids, for round-trip)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Use Cases"
    ws.append(HEADERS)
    for uc in _sorted_use_cases(project):
        ws.append(
            [
                uc.id,
                uc.reference_number or "",
                uc.category or "",
                uc.name or "",
                uc.description or "",
                uc.success_validation or "",
                uc.feature_type.name if uc.feature_type else "",
                uc.status.name if uc.status else "",
                uc.comments or "",
                uc.completed_on.isoformat() if uc.completed_on else "",
            ]
        )
    _finish_sheet(ws)
    return _to_bytes(wb)


def build_template_xlsx(db: Session) -> bytes:
    """A blank template: headers, dropdowns for status/feature, and guidance."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Use Cases"
    ws.append(HEADERS)
    ws["A1"].comment = Comment(
        "Leave 'Id' blank for new use cases. On export it's filled in; keep it to "
        "update an existing use case instead of creating a duplicate.",
        "POC Tracker",
    )
    statuses = [s.name for s in _active(db, UseCaseStatus)]
    features = [f.name for f in _active(db, FeatureType)]
    _add_dropdown(ws, "H", statuses)   # Status column
    _add_dropdown(ws, "G", features)   # Feature Type column
    _finish_sheet(ws)
    return _to_bytes(wb)


def _add_dropdown(ws, col: str, values: list[str]) -> None:
    """Add an Excel list-validation dropdown to a column (rows 2..MAX_ROWS)."""
    joined = ",".join(v for v in values if v and "," not in v)
    if not joined or len(joined) > 250:  # Excel inline-list limit
        return
    dv = DataValidation(type="list", formula1=f'"{joined}"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"{col}2:{col}{MAX_ROWS}")


def _finish_sheet(ws) -> None:
    widths = [6, 16, 20, 30, 48, 36, 16, 18, 28, 14]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"


def _to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sorted_use_cases(project: Project) -> list[ProjectUseCase]:
    return sorted(
        project.use_cases, key=lambda u: (u.category.lower(), u.name.lower())
    )


def _active(db: Session, model) -> list:
    return db.query(model).filter(model.is_active.is_(True)).order_by(model.name).all()


# ---------------------------------------------------------------------------
# Import: parse → classify → apply
# ---------------------------------------------------------------------------


@dataclass
class ImportRow:
    index: int
    action: str  # "new" | "update"
    target_id: int | None
    reference_number: str
    category: str
    name: str
    description: str
    success_validation: str
    comments: str
    status_id: int | None
    status_name: str
    feature_type_id: int | None
    feature_name: str
    completed_on: date | None
    valid: bool
    warnings: list[str] = field(default_factory=list)


class SpreadsheetError(Exception):
    """Raised when a spreadsheet can't be read."""


def parse_spreadsheet(
    filename: str, content: bytes, keys: list[str] | None = None
) -> list[dict]:
    """Parse an .xlsx or .csv into a list of {field_key: str} row dicts.

    ``keys`` is the set of field keys to pull out (defaults to the project
    use-case columns); pass ``LIBRARY_KEYS`` for the library layout.
    """
    keys = keys or KEYS
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return _parse_xlsx(content, keys)
    if name.endswith(".csv") or not name:
        return _parse_csv(content, keys)
    # Unknown extension — try xlsx then csv.
    try:
        return _parse_xlsx(content, keys)
    except SpreadsheetError:
        return _parse_csv(content, keys)


def _normalize(header: str) -> str:
    return (header or "").strip().lower().replace(" ", "_")


def _parse_xlsx(content: bytes, keys: list[str]) -> list[dict]:
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise SpreadsheetError("Could not read that Excel file.") from exc
    ws = wb.worksheets[0]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = next(rows_iter)
    except StopIteration:
        return []
    cols = {_normalize(str(h)): i for i, h in enumerate(header) if h is not None}
    out: list[dict] = []
    for raw in rows_iter:
        row = {k: _cell_str(raw[cols[k]]) if k in cols and cols[k] < len(raw) else "" for k in keys}
        if any(row[k] for k in keys):
            out.append(row)
        if len(out) >= MAX_ROWS:
            break
    wb.close()
    return out


def _parse_csv(content: bytes, keys: list[str]) -> list[dict]:
    text = content.decode("utf-8-sig", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return []
    cols = {_normalize(h): i for i, h in enumerate(header)}
    out: list[dict] = []
    for raw in reader:
        row = {k: (raw[cols[k]].strip() if k in cols and cols[k] < len(raw) else "") for k in keys}
        if any(row[k] for k in keys):
            out.append(row)
        if len(out) >= MAX_ROWS:
            break
    return out


def _cell_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def classify_rows(db: Session, project: Project, rows: list[dict]) -> list[ImportRow]:
    """Resolve names→ids and decide new-vs-update for each parsed row."""
    statuses = {s.name.lower(): s.id for s in _active(db, UseCaseStatus)}
    features = {f.name.lower(): f.id for f in _active(db, FeatureType)}
    existing_ids = {uc.id for uc in project.use_cases}

    results: list[ImportRow] = []
    for i, row in enumerate(rows):
        warnings: list[str] = []
        name = row["name"].strip()
        category = row["category"].strip()

        target_id = int(row["id"]) if row["id"].strip().isdigit() else None
        if target_id is not None and target_id not in existing_ids:
            warnings.append(f"id {target_id} isn't in this project — will be added as new")
            target_id = None
        action = "update" if target_id is not None else "new"

        status_name = row["status"].strip()
        status_id = statuses.get(status_name.lower()) if status_name else None
        if status_name and status_id is None:
            warnings.append(f"unknown status '{status_name}' — will use the default")

        feature_name = row["feature_type"].strip()
        feature_id = features.get(feature_name.lower()) if feature_name else None
        if feature_name and feature_id is None:
            warnings.append(f"unknown feature type '{feature_name}' — will be left blank")

        completed_on = _parse_date_loose(row["completed_on"])

        valid = bool(name and category)
        if not valid:
            warnings.append("missing name or category — row will be skipped")

        results.append(
            ImportRow(
                index=i, action=action, target_id=target_id,
                reference_number=row["reference_number"].strip(),
                category=category, name=name,
                description=row["description"].strip(),
                success_validation=row["success_validation"].strip(),
                comments=row["comments"].strip(),
                status_id=status_id, status_name=status_name,
                feature_type_id=feature_id, feature_name=feature_name,
                completed_on=completed_on, valid=valid, warnings=warnings,
            )
        )
    return results


def _parse_date_loose(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# ===========================================================================
# Library import/export (master template list — no project, no status)
# ===========================================================================

_TRUE_WORDS = {"yes", "y", "true", "1", "active"}
_FALSE_WORDS = {"no", "n", "false", "0", "inactive"}


def _bool_str(value: bool) -> str:
    return "Yes" if value else "No"


def _parse_bool(value: str, default: bool = True) -> bool:
    v = (value or "").strip().lower()
    if v in _TRUE_WORDS:
        return True
    if v in _FALSE_WORDS:
        return False
    return default


def build_library_export_xlsx(db: Session) -> bytes:
    """Export the whole library as an .xlsx (with ids, for round-trip update)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Library"
    ws.append(LIBRARY_HEADERS)
    entries = (
        db.query(UseCaseLibrary)
        .order_by(
            UseCaseLibrary.category,
            UseCaseLibrary.default_reference_number,
            UseCaseLibrary.name,
        )
        .all()
    )
    for e in entries:
        ws.append(
            [
                e.id,
                e.default_reference_number or "",
                e.category or "",
                e.name or "",
                e.description or "",
                e.success_validation or "",
                e.feature_type.name if e.feature_type else "",
                _bool_str(e.is_active),
            ]
        )
    _finish_library_sheet(ws)
    return _to_bytes(wb)


def build_library_template_xlsx(db: Session) -> bytes:
    """A blank library template: headers, a Feature Type dropdown, and guidance."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Library"
    ws.append(LIBRARY_HEADERS)
    ws["A1"].comment = Comment(
        "Leave 'Id' blank for new entries. On export it's filled in; keep it to "
        "update an existing library entry instead of creating a duplicate.",
        "POC Tracker",
    )
    _add_dropdown(ws, "G", [f.name for f in _active(db, FeatureType)])  # Feature Type
    _add_dropdown(ws, "H", ["Yes", "No"])  # Active
    _finish_library_sheet(ws)
    return _to_bytes(wb)


def _finish_library_sheet(ws) -> None:
    widths = [6, 16, 20, 30, 48, 36, 16, 10]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"


@dataclass
class LibraryImportRow:
    index: int
    action: str  # "new" | "update"
    target_id: int | None
    reference_number: str
    category: str
    name: str
    description: str
    success_validation: str
    feature_type_id: int | None
    feature_name: str
    is_active: bool
    valid: bool
    warnings: list[str] = field(default_factory=list)


def classify_library_rows(db: Session, rows: list[dict]) -> list[LibraryImportRow]:
    """Resolve feature-type names→ids and decide new-vs-update for each row."""
    features = {f.name.lower(): f.id for f in _active(db, FeatureType)}
    existing_ids = {eid for (eid,) in db.query(UseCaseLibrary.id).all()}

    results: list[LibraryImportRow] = []
    for i, row in enumerate(rows):
        warnings: list[str] = []
        name = row["name"].strip()
        category = row["category"].strip()

        target_id = int(row["id"]) if row["id"].strip().isdigit() else None
        if target_id is not None and target_id not in existing_ids:
            warnings.append(f"id {target_id} isn't in the library — will be added as new")
            target_id = None
        action = "update" if target_id is not None else "new"

        feature_name = row["feature_type"].strip()
        feature_id = features.get(feature_name.lower()) if feature_name else None
        if feature_name and feature_id is None:
            warnings.append(f"unknown feature type '{feature_name}' — will be left blank")

        valid = bool(name and category)
        if not valid:
            warnings.append("missing name or category — row will be skipped")

        results.append(
            LibraryImportRow(
                index=i, action=action, target_id=target_id,
                reference_number=row["reference_number"].strip(),
                category=category, name=name,
                description=row["description"].strip(),
                success_validation=row["success_validation"].strip(),
                feature_type_id=feature_id, feature_name=feature_name,
                is_active=_parse_bool(row["active"], default=True),
                valid=valid, warnings=warnings,
            )
        )
    return results
