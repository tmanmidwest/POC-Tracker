"""The reporting field registry — the whitelist that makes user-built reports safe.

This is the load-bearing safety boundary for the whole reporting layer, and it is
deliberately **pure**: no SQLAlchemy import, no session, nothing but dataclasses
and read helpers (same shape as ``app.services.rbac.capabilities``). A report
definition (columns, filters, group-by, measures, sort) is validated *entirely*
against this catalog before any query runs — a field that isn't registered, or an
operator a field doesn't allow, is rejected here, so there is no way for a
user-authored definition to reach the database as anything but a whitelisted
shape.

Each entity (``project``, ``task``, ``use_case``) has an ordered list of
:class:`ReportField`. The *engine* (``app.services.reporting.engine``) holds the
matching value getters and the region-scoped query — this module only describes
what is reportable and how it may be used, so it stays trivially importable and
testable.

**``source``** distinguishes real columns from *derived* qualities computed from
``app.services.insights`` (completion %, at-risk, cycle time, …). Derived fields
can't be pushed into SQL WHERE/ORDER, so the engine evaluates them in Python over
the bounded result set; the UI surfaces the distinction so expectations stay
honest.

**``indexed``** marks fields backed by an indexed column. v1 evaluates field
filters in Python over the region-scoped, capped set (safe at this app's scale),
so ``indexed`` is currently informational — it marks exactly which filters/sorts
can be pushed down to SQL when data volume grows, without re-deriving the safety
analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

# --- Entities ----------------------------------------------------------------

ENTITY_PROJECT = "project"
ENTITY_TASK = "task"
ENTITY_USE_CASE = "use_case"
ENTITIES: tuple[str, ...] = (ENTITY_PROJECT, ENTITY_TASK, ENTITY_USE_CASE)

ENTITY_LABELS: dict[str, str] = {
    ENTITY_PROJECT: "Projects",
    ENTITY_TASK: "Tasks",
    ENTITY_USE_CASE: "Use cases",
}

# --- Field types -------------------------------------------------------------

TYPE_STRING = "string"
TYPE_NUMBER = "number"
TYPE_DATE = "date"
TYPE_DATETIME = "datetime"
TYPE_ENUM = "enum"
TYPE_BOOL = "bool"

# --- Sources -----------------------------------------------------------------

SOURCE_COLUMN = "column"
SOURCE_DERIVED = "derived"

# --- Operators ---------------------------------------------------------------
# The full operator vocabulary. A filter is accepted only if its ``op`` is one of
# these *and* is listed in the target field's ``ops``.

OP_EQ = "eq"
OP_NE = "ne"
OP_IN = "in"
OP_NOT_IN = "not_in"
OP_CONTAINS = "contains"
OP_GT = "gt"
OP_GTE = "gte"
OP_LT = "lt"
OP_LTE = "lte"
OP_BETWEEN = "between"
OP_IS_EMPTY = "is_empty"
OP_IS_NOT_EMPTY = "is_not_empty"
OP_IS_TRUE = "is_true"
OP_IS_FALSE = "is_false"

ALL_OPS: frozenset[str] = frozenset(
    {
        OP_EQ, OP_NE, OP_IN, OP_NOT_IN, OP_CONTAINS,
        OP_GT, OP_GTE, OP_LT, OP_LTE, OP_BETWEEN,
        OP_IS_EMPTY, OP_IS_NOT_EMPTY, OP_IS_TRUE, OP_IS_FALSE,
    }
)

# Human labels for the builder UI.
OP_LABELS: dict[str, str] = {
    OP_EQ: "is",
    OP_NE: "is not",
    OP_IN: "is any of",
    OP_NOT_IN: "is none of",
    OP_CONTAINS: "contains",
    OP_GT: "greater than",
    OP_GTE: "at least",
    OP_LT: "less than",
    OP_LTE: "at most",
    OP_BETWEEN: "between",
    OP_IS_EMPTY: "is empty",
    OP_IS_NOT_EMPTY: "is not empty",
    OP_IS_TRUE: "is yes",
    OP_IS_FALSE: "is no",
}

# Default operator sets per type. A field may override with its own ``ops``.
_DEFAULT_OPS: dict[str, tuple[str, ...]] = {
    TYPE_STRING: (OP_CONTAINS, OP_EQ, OP_NE, OP_IS_EMPTY, OP_IS_NOT_EMPTY),
    TYPE_ENUM: (OP_EQ, OP_NE, OP_IN, OP_NOT_IN),
    TYPE_NUMBER: (OP_EQ, OP_NE, OP_GT, OP_GTE, OP_LT, OP_LTE, OP_BETWEEN),
    TYPE_DATE: (OP_EQ, OP_GT, OP_GTE, OP_LT, OP_LTE, OP_BETWEEN, OP_IS_EMPTY, OP_IS_NOT_EMPTY),
    TYPE_DATETIME: (OP_GT, OP_GTE, OP_LT, OP_LTE, OP_BETWEEN),
    TYPE_BOOL: (OP_IS_TRUE, OP_IS_FALSE),
}

# Aggregations valid for a numeric measure in a summary report (count is implicit).
MEASURE_FNS: tuple[str, ...] = ("avg", "sum", "min", "max")
MEASURE_LABELS: dict[str, str] = {
    "count": "Count",
    "avg": "Average",
    "sum": "Sum",
    "min": "Min",
    "max": "Max",
}


@dataclass(frozen=True)
class ReportField:
    """One reportable field on an entity.

    ``key``        — stable slug, unique within its entity (stored in the report
                     definition, e.g. ``status``, ``completion_pct``).
    ``label``      — human label for the builder + column header.
    ``type``       — one of the ``TYPE_*`` constants; drives operators + formatting.
    ``source``     — ``column`` (a real DB column) or ``derived`` (computed from
                     ``insights`` in Python).
    ``ops``        — allowed operators (defaults from the type when not given).
    ``filterable`` / ``groupable`` / ``sortable`` — where the field may be used.
    ``indexed``    — backed by an indexed column (informational in v1; marks
                     push-down-safe filters/sorts for later).
    ``needs_index``— a column filter/sort we'd want a new Postgres index for at
                     scale (surfaced so migration 0043's index list is intentional).
    ``help``       — optional one-liner shown in the field picker.
    """

    key: str
    label: str
    type: str
    source: str = SOURCE_COLUMN
    ops: tuple[str, ...] = ()
    filterable: bool = True
    groupable: bool = False
    sortable: bool = True
    indexed: bool = False
    needs_index: bool = False
    help: str = ""

    def __post_init__(self) -> None:
        # Fill default operators from the type when the field didn't specify any.
        if not self.ops:
            object.__setattr__(self, "ops", _DEFAULT_OPS.get(self.type, (OP_EQ,)))

    @property
    def is_numeric(self) -> bool:
        return self.type == TYPE_NUMBER

    @property
    def is_measurable(self) -> bool:
        """Numeric fields can be aggregated (avg/sum/min/max) in a summary."""
        return self.type == TYPE_NUMBER


def _f(*args, **kwargs) -> ReportField:  # tiny constructor alias for a dense table
    return ReportField(*args, **kwargs)


# --- The catalog -------------------------------------------------------------
# Ordering here is the display order in the builder's field picker. Keep columns
# first, derived fields last, within each entity.

_PROJECT_FIELDS: tuple[ReportField, ...] = (
    _f("customer", "Customer", TYPE_STRING, ops=(OP_CONTAINS, OP_EQ, OP_NE),
       groupable=True, indexed=True, help="Customer the POC is for."),
    _f("name", "Project name", TYPE_STRING, help="The POC's own name (falls back to the customer)."),
    _f("status", "Status", TYPE_ENUM, groupable=True, indexed=True),
    _f("outcome", "Outcome", TYPE_ENUM, groupable=True,
       help="Win/loss outcome derived from the status (won / lost / no decision / none)."),
    _f("type", "Type", TYPE_ENUM, groupable=True, indexed=True,
       help="Engagement type (Workshop, POC Full Stack, …)."),
    _f("region", "Region", TYPE_ENUM, groupable=True, indexed=True),
    _f("sales_engineer", "Sales engineer", TYPE_ENUM, groupable=True, indexed=True),
    _f("account_executive", "Account executive", TYPE_STRING),
    _f("competitor", "Competitor", TYPE_STRING, groupable=True,
       help="Who we were up against (mostly on lost deals)."),
    _f("close_reason", "Close reason", TYPE_ENUM, groupable=True, indexed=True),
    _f("start_date", "Start date", TYPE_DATE, needs_index=True),
    _f("end_date", "End date", TYPE_DATE, needs_index=True),
    _f("closed_date", "Closed date", TYPE_DATE, needs_index=True),
    _f("is_archived", "Archived", TYPE_BOOL, groupable=True, indexed=True),
    _f("created_at", "Created", TYPE_DATETIME),
    _f("updated_at", "Last updated", TYPE_DATETIME),
    # -- derived (computed from insights, evaluated in Python) --
    _f("completion_pct", "Completion %", TYPE_NUMBER, source=SOURCE_DERIVED,
       help="Percent of use cases in a complete status."),
    _f("use_case_total", "Use cases (total)", TYPE_NUMBER, source=SOURCE_DERIVED),
    _f("use_case_done", "Use cases (complete)", TYPE_NUMBER, source=SOURCE_DERIVED),
    _f("is_at_risk", "At risk", TYPE_BOOL, source=SOURCE_DERIVED, groupable=True,
       help="Past its end date with incomplete use cases."),
    _f("is_stalled", "Stalled", TYPE_BOOL, source=SOURCE_DERIVED, groupable=True,
       help="No update in 14+ days."),
    _f("is_off_track", "Off track", TYPE_BOOL, source=SOURCE_DERIVED, groupable=True,
       help="Has an overdue, incomplete milestone."),
    _f("cycle_time_days", "Cycle time (days)", TYPE_NUMBER, source=SOURCE_DERIVED,
       help="Days from start to close (closed deals only)."),
    _f("idle_days", "Idle (days)", TYPE_NUMBER, source=SOURCE_DERIVED,
       help="Whole days since the project was last touched."),
)

_TASK_FIELDS: tuple[ReportField, ...] = (
    _f("title", "Title", TYPE_STRING),
    _f("status", "Status", TYPE_ENUM, groupable=True, indexed=True),
    _f("priority", "Priority", TYPE_ENUM, groupable=True, indexed=True),
    _f("owner", "Owner", TYPE_ENUM, groupable=True, indexed=True),
    _f("project", "Project", TYPE_STRING, groupable=True,
       help="The POC this task is attached to, if any."),
    _f("customer", "Customer", TYPE_STRING, groupable=True,
       help="Customer of the attached project."),
    _f("start_date", "Start date", TYPE_DATE, needs_index=True),
    _f("due_date", "Due date", TYPE_DATE, needs_index=True),
    _f("is_archived", "Archived", TYPE_BOOL, groupable=True, indexed=True),
    _f("created_at", "Created", TYPE_DATETIME),
    _f("updated_at", "Last updated", TYPE_DATETIME),
    # -- derived --
    _f("is_overdue", "Overdue", TYPE_BOOL, source=SOURCE_DERIVED, groupable=True,
       help="Past its due date and not complete."),
)

_USE_CASE_FIELDS: tuple[ReportField, ...] = (
    _f("name", "Name", TYPE_STRING),
    _f("category", "Category", TYPE_STRING, groupable=True, indexed=True),
    _f("status", "Status", TYPE_ENUM, groupable=True, indexed=True),
    _f("feature_type", "Feature type", TYPE_ENUM, groupable=True, indexed=True),
    _f("source", "Source", TYPE_ENUM, groupable=True,
       help="Where the use case came from — library or custom."),
    _f("library_set", "Library set", TYPE_ENUM, groupable=True,
       help="The library this snapshot came from, if any."),
    _f("project", "Project", TYPE_STRING, groupable=True),
    _f("customer", "Customer", TYPE_STRING, groupable=True),
    _f("region", "Region", TYPE_ENUM, groupable=True,
       help="Region of the parent project."),
    _f("reference_number", "Reference #", TYPE_STRING),
    _f("completed_on", "Completed on", TYPE_DATE, needs_index=True),
    _f("created_at", "Created", TYPE_DATETIME),
    _f("updated_at", "Last updated", TYPE_DATETIME),
    # -- derived --
    _f("is_complete", "Complete", TYPE_BOOL, source=SOURCE_DERIVED, groupable=True,
       help="In a status that counts as complete."),
)

ENTITY_FIELDS: dict[str, tuple[ReportField, ...]] = {
    ENTITY_PROJECT: _PROJECT_FIELDS,
    ENTITY_TASK: _TASK_FIELDS,
    ENTITY_USE_CASE: _USE_CASE_FIELDS,
}

# Per-entity fast lookup: key -> ReportField.
_BY_ENTITY_KEY: dict[str, dict[str, ReportField]] = {
    entity: {f.key: f for f in fields} for entity, fields in ENTITY_FIELDS.items()
}


# --- Read helpers ------------------------------------------------------------


def is_valid_entity(entity: str | None) -> bool:
    return entity in _BY_ENTITY_KEY


def fields_for(entity: str) -> tuple[ReportField, ...]:
    """All reportable fields for an entity, in display order."""
    return ENTITY_FIELDS.get(entity, ())


def get_field(entity: str, key: str) -> ReportField | None:
    """The :class:`ReportField` for ``(entity, key)`` or ``None`` if unknown."""
    return _BY_ENTITY_KEY.get(entity, {}).get(key)


def filterable_fields(entity: str) -> list[ReportField]:
    return [f for f in fields_for(entity) if f.filterable]


def groupable_fields(entity: str) -> list[ReportField]:
    return [f for f in fields_for(entity) if f.groupable]


def measurable_fields(entity: str) -> list[ReportField]:
    return [f for f in fields_for(entity) if f.is_measurable]


# --- Definition validation ---------------------------------------------------
# A report *definition* is the JSON a user builds:
#   {
#     "columns":  [field_key, ...],
#     "filters":  [{"field": key, "op": op, "value": ...}, ...],
#     "group_by": field_key | null,
#     "measures": [{"fn": "avg"|"sum"|..., "field": key}, ...],
#     "sort":     [{"field": key, "dir": "asc"|"desc"}, ...],
#   }
# ``summary`` (tabular vs grouped) lives on the SavedReport row, not the JSON.


class DefinitionError(ValueError):
    """A report definition referenced an unknown field or an operator a field
    doesn't allow. Raised before any query runs so a bad definition can never
    reach the database."""


def validate_definition(entity: str, definition: dict, *, summary: bool) -> dict:
    """Validate + normalize a report definition against the registry.

    Returns a cleaned definition (unknown keys dropped, structure normalized).
    Raises :class:`DefinitionError` on anything the registry doesn't allow — an
    unknown entity/field, a disallowed operator, a non-groupable group-by, a
    non-numeric measure, or a summary with no group-by and no measures.
    """
    if not is_valid_entity(entity):
        raise DefinitionError(f"Unknown report entity {entity!r}.")
    if not isinstance(definition, dict):
        raise DefinitionError("Report definition must be an object.")

    # -- columns --
    raw_columns = definition.get("columns") or []
    columns: list[str] = []
    for key in raw_columns:
        f = get_field(entity, key)
        if f is None:
            raise DefinitionError(f"Unknown column {key!r} for {entity}.")
        if key not in columns:
            columns.append(key)

    # -- filters --
    filters: list[dict] = []
    for flt in definition.get("filters") or []:
        if not isinstance(flt, dict):
            raise DefinitionError("Each filter must be an object.")
        key = flt.get("field")
        op = flt.get("op")
        f = get_field(entity, key)
        if f is None:
            raise DefinitionError(f"Unknown filter field {key!r} for {entity}.")
        if not f.filterable:
            raise DefinitionError(f"Field {key!r} is not filterable.")
        if op not in ALL_OPS or op not in f.ops:
            raise DefinitionError(f"Operator {op!r} is not allowed on {key!r}.")
        filters.append({"field": key, "op": op, "value": flt.get("value")})

    # -- group_by --
    group_by = definition.get("group_by") or None
    if group_by is not None:
        f = get_field(entity, group_by)
        if f is None or not f.groupable:
            raise DefinitionError(f"Field {group_by!r} cannot be grouped by.")

    # -- measures --
    measures: list[dict] = []
    for m in definition.get("measures") or []:
        if not isinstance(m, dict):
            raise DefinitionError("Each measure must be an object.")
        fn = m.get("fn")
        key = m.get("field")
        if fn not in MEASURE_FNS:
            raise DefinitionError(f"Unknown measure function {fn!r}.")
        f = get_field(entity, key)
        if f is None or not f.is_measurable:
            raise DefinitionError(f"Field {key!r} cannot be measured with {fn!r}.")
        measures.append({"fn": fn, "field": key})

    # -- sort --
    sort: list[dict] = []
    for s in definition.get("sort") or []:
        if not isinstance(s, dict):
            raise DefinitionError("Each sort must be an object.")
        key = s.get("field")
        direction = "desc" if str(s.get("dir")).lower() == "desc" else "asc"
        f = get_field(entity, key)
        if f is None or not f.sortable:
            raise DefinitionError(f"Field {key!r} cannot be sorted by.")
        sort.append({"field": key, "dir": direction})

    if summary and group_by is None and not measures:
        raise DefinitionError(
            "A summary report needs a group-by field or at least one measure."
        )
    if not summary and not columns:
        raise DefinitionError("A report needs at least one column.")

    return {
        "columns": columns,
        "filters": filters,
        "group_by": group_by,
        "measures": measures,
        "sort": sort,
    }


# --- Import-time integrity ---------------------------------------------------
# Turn a typo in the catalog into an immediate failure rather than a field that
# silently never matches or an operator the type doesn't understand.

for _entity, _fields in ENTITY_FIELDS.items():  # pragma: no cover - guard
    _seen: set[str] = set()
    for _fld in _fields:
        if _fld.key in _seen:
            raise RuntimeError(f"Duplicate field key {_fld.key!r} in {_entity}")
        _seen.add(_fld.key)
        _bad = set(_fld.ops) - ALL_OPS
        if _bad:
            raise RuntimeError(f"Field {_entity}.{_fld.key} has unknown ops {_bad}")
        if _fld.source not in (SOURCE_COLUMN, SOURCE_DERIVED):
            raise RuntimeError(f"Field {_entity}.{_fld.key} has bad source")
