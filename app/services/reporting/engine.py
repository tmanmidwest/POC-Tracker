"""The reporting engine: turn a validated definition into region-scoped results.

This is the runtime half of the registry. Given ``(entity, definition)`` and the
requesting user it:

1. **Loads a region-scoped, hard-capped row set from SQL** — the *only* place the
   security boundary is enforced. Project/use-case rows are intersected with
   ``access.accessible_project_ids``; tasks are the user's own unless they hold
   ``task.view_all``. Nothing downstream can widen this.
2. **Projects each row through the registry's value getters** — real columns and
   derived ``insights`` qualities alike.
3. **Applies field filters, sorting, grouping, and aggregation in Python** over
   that bounded set. At this app's scale (hundreds of rows) this is safe and
   keeps derived fields (completion %, at-risk, cycle time) first-class; the
   registry's ``indexed`` flags mark what can be pushed to SQL when data grows.

The result set is **recomputed per viewer every run** — it is never stored — so a
shared or published report can never leak rows across regions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime

from sqlalchemy.orm import Session, selectinload

from app.models import AppUser, Project, ProjectUseCase, Region, Task
from app.services import access, insights
from app.services.regions import get_user_region_ids
from app.services.reporting import registry as reg

# --- Stability caps ----------------------------------------------------------
# The hard ceiling on rows pulled from SQL for a single run. Region scope already
# bounds a user to their own projects; this backstops a pathological definition.
MAX_ROWS = 5000
# Rows shown in the live builder preview (the full set still exports).
PREVIEW_LIMIT = 50
# Rows shown per page in the run view before "export for the rest".
PAGE_LIMIT = 500

#: Bound how many heavy report renders run at once (exports/scheduled sends),
#: kept off the event loop by the callers. Shared across the app.
RENDER_SEMAPHORE = asyncio.Semaphore(2)


# ---------------------------------------------------------------------------
# Value getters — key -> callable(orm_obj) -> raw value
# ---------------------------------------------------------------------------


def _uc_complete(uc: ProjectUseCase) -> bool:
    return bool(uc.status and uc.status.is_complete_status)


def _task_complete(t: Task) -> bool:
    return bool(t.status and t.status.is_terminal)


def _task_overdue(t: Task, today: date) -> bool:
    return t.due_date is not None and t.due_date < today and not _task_complete(t)


_PROJECT_GETTERS: dict[str, Callable[[Project], object]] = {
    "customer": lambda p: p.customer.name if p.customer else None,
    "name": lambda p: p.display_name,
    "status": lambda p: p.status.name if p.status else None,
    "outcome": lambda p: insights.outcome(p),
    "type": lambda p: p.type.name if p.type else None,
    "region": lambda p: p.region.name if p.region else None,
    "sales_engineer": lambda p: p.sales_engineer.display_label if p.sales_engineer else None,
    "account_executive": lambda p: p.account_executive,
    "competitor": lambda p: p.competitor,
    "close_reason": lambda p: p.close_reason.name if p.close_reason else None,
    "start_date": lambda p: p.start_date,
    "end_date": lambda p: p.end_date,
    "closed_date": lambda p: p.closed_date,
    "is_archived": lambda p: bool(p.is_archived),
    "created_at": lambda p: p.created_at,
    "updated_at": lambda p: p.updated_at,
    "completion_pct": lambda p: insights.completion_pct(p),
    "use_case_total": lambda p: len(p.use_cases),
    "use_case_done": lambda p: insights.completed_use_cases(p),
    "is_at_risk": lambda p: insights.is_at_risk(p),
    "is_stalled": lambda p: insights.is_stalled(p),
    "is_off_track": lambda p: insights.is_off_track(p),
    "cycle_time_days": lambda p: insights.cycle_time_days(p),
    "idle_days": lambda p: insights.idle_days(p.updated_at),
}

_TASK_GETTERS: dict[str, Callable[[Task], object]] = {
    "title": lambda t: t.title,
    "status": lambda t: t.status.name if t.status else None,
    "priority": lambda t: t.priority.name if t.priority else None,
    "owner": lambda t: t.owner.display_label if t.owner else None,
    "project": lambda t: t.project.display_name if t.project else None,
    "customer": lambda t: (t.project.customer.name if t.project and t.project.customer else None),
    "start_date": lambda t: t.start_date,
    "due_date": lambda t: t.due_date,
    "is_archived": lambda t: bool(t.is_archived),
    "created_at": lambda t: t.created_at,
    "updated_at": lambda t: t.updated_at,
    # is_overdue needs "today"; bound at run time (see _getters_for).
}

_USE_CASE_GETTERS: dict[str, Callable[[ProjectUseCase], object]] = {
    "name": lambda uc: uc.name,
    "category": lambda uc: uc.category,
    "status": lambda uc: uc.status.name if uc.status else None,
    "feature_type": lambda uc: uc.feature_type.name if uc.feature_type else None,
    "source": lambda uc: uc.source,
    "library_set": lambda uc: uc.library_set.name if uc.library_set else None,
    "project": lambda uc: uc.project.display_name if uc.project else None,
    "customer": lambda uc: (uc.project.customer.name if uc.project and uc.project.customer else None),
    "region": lambda uc: (uc.project.region.name if uc.project and uc.project.region else None),
    "reference_number": lambda uc: uc.reference_number,
    "completed_on": lambda uc: uc.completed_on,
    "created_at": lambda uc: uc.created_at,
    "updated_at": lambda uc: uc.updated_at,
    "is_complete": _uc_complete,
}


def _getters_for(entity: str, today: date) -> dict[str, Callable[[object], object]]:
    """The value getters for an entity, with any date-relative ones bound."""
    if entity == reg.ENTITY_PROJECT:
        return dict(_PROJECT_GETTERS)  # type: ignore[arg-type]
    if entity == reg.ENTITY_USE_CASE:
        return dict(_USE_CASE_GETTERS)  # type: ignore[arg-type]
    if entity == reg.ENTITY_TASK:
        g = dict(_TASK_GETTERS)
        g["is_overdue"] = lambda t: _task_overdue(t, today)  # type: ignore[index]
        return g  # type: ignore[return-value]
    return {}


# ---------------------------------------------------------------------------
# Region-scoped loading — the security boundary
# ---------------------------------------------------------------------------


def _load_rows(db: Session, user: AppUser, entity: str) -> tuple[list, bool]:
    """Load the region-scoped, capped ORM rows for an entity.

    Returns ``(rows, capped)`` where ``capped`` is True if the hard cap was hit
    (so the run view can warn that filters/aggregates cover only the first
    ``MAX_ROWS``).
    """
    if entity == reg.ENTITY_PROJECT:
        q = db.query(Project).options(
            selectinload(Project.use_cases),
            selectinload(Project.milestones),
        )
        allowed = access.accessible_project_ids(db, user)
        if allowed is not None:
            q = q.filter(Project.id.in_(allowed))
        rows = q.order_by(Project.id).limit(MAX_ROWS + 1).all()

    elif entity == reg.ENTITY_USE_CASE:
        q = db.query(ProjectUseCase).options(selectinload(ProjectUseCase.project))
        allowed = access.accessible_project_ids(db, user)
        if allowed is not None:
            q = q.filter(ProjectUseCase.project_id.in_(allowed))
        rows = q.order_by(ProjectUseCase.id).limit(MAX_ROWS + 1).all()

    elif entity == reg.ENTITY_TASK:
        q = db.query(Task).options(selectinload(Task.project))
        # Tasks are user-owned; only task.view_all sees everyone's.
        if not user.can("task.view_all"):
            q = q.filter(Task.owner_user_id == user.id)
        rows = q.order_by(Task.id).limit(MAX_ROWS + 1).all()

    else:
        return [], False

    capped = len(rows) > MAX_ROWS
    return rows[:MAX_ROWS], capped


# ---------------------------------------------------------------------------
# Filtering / sorting / formatting (Python, over the bounded set)
# ---------------------------------------------------------------------------


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _as_number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ci(value: object) -> str:
    return str(value).strip().lower()


def _match(field: reg.ReportField, op: str, raw: object, value: object) -> bool:
    """Whether a single row's ``raw`` value satisfies ``op value`` for ``field``."""
    if op == reg.OP_IS_EMPTY:
        return raw is None or raw == ""
    if op == reg.OP_IS_NOT_EMPTY:
        return not (raw is None or raw == "")
    if op == reg.OP_IS_TRUE:
        return bool(raw) is True
    if op == reg.OP_IS_FALSE:
        return not bool(raw)

    if raw is None:
        return False  # any positive comparison against a missing value fails

    if field.type in (reg.TYPE_DATE, reg.TYPE_DATETIME):
        rv = _as_date(raw)
        if rv is None:
            return False
        if op == reg.OP_BETWEEN:
            lo, hi = (value or [None, None])[:2] if isinstance(value, (list, tuple)) else (None, None)
            lo_d, hi_d = _as_date(lo), _as_date(hi)
            return (lo_d is None or rv >= lo_d) and (hi_d is None or rv <= hi_d)
        cv = _as_date(value)
        if cv is None:
            return False
        return _cmp(op, rv, cv)

    if field.type == reg.TYPE_NUMBER:
        rv = _as_number(raw)
        if rv is None:
            return False
        if op == reg.OP_BETWEEN:
            lo, hi = (value or [None, None])[:2] if isinstance(value, (list, tuple)) else (None, None)
            lo_n, hi_n = _as_number(lo), _as_number(hi)
            return (lo_n is None or rv >= lo_n) and (hi_n is None or rv <= hi_n)
        cv = _as_number(value)
        if cv is None:
            return False
        return _cmp(op, rv, cv)

    # string / enum
    if op == reg.OP_CONTAINS:
        return _ci(value) in _ci(raw)
    if op == reg.OP_EQ:
        return _ci(raw) == _ci(value)
    if op == reg.OP_NE:
        return _ci(raw) != _ci(value)
    if op == reg.OP_IN:
        vals = value if isinstance(value, (list, tuple)) else [value]
        return _ci(raw) in {_ci(v) for v in vals}
    if op == reg.OP_NOT_IN:
        vals = value if isinstance(value, (list, tuple)) else [value]
        return _ci(raw) not in {_ci(v) for v in vals}
    return False


def _cmp(op: str, a, b) -> bool:
    if op in (reg.OP_EQ,):
        return a == b
    if op == reg.OP_NE:
        return a != b
    if op == reg.OP_GT:
        return a > b
    if op == reg.OP_GTE:
        return a >= b
    if op == reg.OP_LT:
        return a < b
    if op == reg.OP_LTE:
        return a <= b
    return False


def format_value(field: reg.ReportField, raw: object) -> str:
    """Human display for a raw value (used by the table and the exporters)."""
    if raw is None or raw == "":
        return "—"
    if field.type == reg.TYPE_BOOL:
        return "Yes" if raw else "No"
    if field.type == reg.TYPE_DATE:
        d = _as_date(raw)
        return d.isoformat() if d else "—"
    if field.type == reg.TYPE_DATETIME:
        d = _as_date(raw)
        return d.isoformat() if d else "—"
    if field.type == reg.TYPE_NUMBER:
        n = _as_number(raw)
        if n is None:
            return "—"
        text = str(int(n)) if float(n).is_integer() else str(round(n, 1))
        return f"{text}%" if field.key.endswith("_pct") else text
    if field.key == "outcome":
        return {"won": "Won", "lost": "Lost", "no_decision": "No decision",
                "none": "—"}.get(str(raw), str(raw))
    return str(raw)


def _sort_key_factory(fields: list[tuple[reg.ReportField, str]]):
    """Build a stable sort key over (field, dir) pairs; None sorts last."""

    def key(projected: dict):
        parts = []
        for f, direction in fields:
            raw = projected.get(f.key)
            missing = raw is None
            if f.type in (reg.TYPE_DATE, reg.TYPE_DATETIME):
                val = _as_date(raw) or date.min
            elif f.type == reg.TYPE_NUMBER:
                val = _as_number(raw)
                val = val if val is not None else float("-inf")
            elif f.type == reg.TYPE_BOOL:
                val = 1 if raw else 0
            else:
                val = _ci(raw) if raw is not None else ""
            # For descending, invert comparables where possible.
            parts.append((missing, _Rev(val) if direction == "desc" else val))
        return parts

    return key


class _Rev:
    """Wrapper that reverses ordering for a single sort component."""

    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        return other.v < self.v

    def __eq__(self, other):
        return self.v == other.v


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class RowView:
    cells: list[str]           # formatted, aligned to result.columns
    raw: dict                  # field_key -> raw value (for export)


@dataclass
class GroupRow:
    label: str
    count: int
    measures: list[str]        # formatted, aligned to result.measure_headers


@dataclass
class ScopeInfo:
    region_scoped: bool
    region_names: list[str]
    region_count: int
    total_regions: int


@dataclass
class RunResult:
    entity: str
    is_summary: bool
    columns: list[reg.ReportField] = dc_field(default_factory=list)
    rows: list[RowView] = dc_field(default_factory=list)
    group_field: reg.ReportField | None = None
    measure_headers: list[str] = dc_field(default_factory=list)
    group_rows: list[GroupRow] = dc_field(default_factory=list)
    total_matched: int = 0
    loaded: int = 0
    capped: bool = False
    scope: ScopeInfo | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scope_info(db: Session, user: AppUser) -> ScopeInfo:
    """Region-scope summary for the run view's "seeing N of M regions" chip."""
    total = db.query(Region).count()
    if not access.region_scoped(user):
        return ScopeInfo(False, [], 0, total)
    ids = get_user_region_ids(db, user.id)
    names = [r.name for r in db.query(Region).filter(Region.id.in_(ids)).order_by(Region.name)] if ids else []
    return ScopeInfo(True, names, len(names), total)


def enum_options(db: Session, user: AppUser, entity: str, field_key: str) -> list[str]:
    """Distinct values for a filter/group field, within the user's region scope.

    Derived from the region-scoped row set so options always match both the data
    and what the user is allowed to see.
    """
    field = reg.get_field(entity, field_key)
    if field is None:
        return []
    rows, _ = _load_rows(db, user, entity)
    getter = _getters_for(entity, date.today()).get(field_key)
    if getter is None:
        return []
    seen: set[str] = set()
    for obj in rows:
        raw = getter(obj)
        if raw is None or raw == "":
            continue
        seen.add(format_value(field, raw) if field.type != reg.TYPE_ENUM else str(raw))
    return sorted(seen, key=str.lower)


def run(
    db: Session,
    user: AppUser,
    entity: str,
    definition: dict,
    *,
    is_summary: bool,
    limit: int | None = None,
) -> RunResult:
    """Run a validated report definition for ``user`` and return a RunResult.

    ``definition`` must already be validated/normalized by
    :func:`registry.validate_definition`. ``limit`` truncates the displayed rows
    (tabular only); ``None`` means up to :data:`PAGE_LIMIT`.
    """
    today = date.today()
    getters = _getters_for(entity, today)
    objs, capped = _load_rows(db, user, entity)

    # Project every loaded row to {field_key: raw} for all entity fields, so
    # filters/sorts/measures over any field are uniform (columns + derived).
    all_fields = reg.fields_for(entity)
    projected: list[dict] = []
    for obj in objs:
        row = {f.key: getters[f.key](obj) for f in all_fields if f.key in getters}
        projected.append(row)

    # -- filter (Python) --
    for flt in definition.get("filters", []):
        f = reg.get_field(entity, flt["field"])
        if f is None:
            continue
        op, value = flt["op"], flt.get("value")
        projected = [r for r in projected if _match(f, op, r.get(f.key), value)]

    total_matched = len(projected)

    result = RunResult(
        entity=entity,
        is_summary=is_summary,
        total_matched=total_matched,
        loaded=len(objs),
        capped=capped,
        scope=scope_info(db, user),
    )

    if is_summary:
        _summarize(result, entity, definition, projected)
        return result

    # -- tabular --
    sort_specs = [
        (reg.get_field(entity, s["field"]), s["dir"])
        for s in definition.get("sort", [])
        if reg.get_field(entity, s["field"])
    ]
    if sort_specs:
        projected.sort(key=_sort_key_factory(sort_specs))  # type: ignore[arg-type]

    columns = [reg.get_field(entity, k) for k in definition.get("columns", [])]
    columns = [c for c in columns if c is not None]
    result.columns = columns

    page = limit if limit is not None else PAGE_LIMIT
    for r in projected[:page]:
        cells = [format_value(c, r.get(c.key)) for c in columns]
        result.rows.append(RowView(cells=cells, raw={c.key: r.get(c.key) for c in columns}))
    return result


def _summarize(result: RunResult, entity: str, definition: dict, projected: list[dict]) -> None:
    """Group + aggregate the filtered rows into ``result`` (summary reports)."""
    group_by = definition.get("group_by")
    gfield = reg.get_field(entity, group_by) if group_by else None
    result.group_field = gfield

    measures = definition.get("measures", [])
    mfields = [(m["fn"], reg.get_field(entity, m["field"])) for m in measures]
    mfields = [(fn, f) for fn, f in mfields if f is not None]
    result.measure_headers = ["Count"] + [
        f"{reg.MEASURE_LABELS.get(fn, fn)} {f.label}" for fn, f in mfields
    ]

    # Bucket rows by the group value's display (or a single "All" bucket).
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in projected:
        label = format_value(gfield, r.get(gfield.key)) if gfield else "All"
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(r)

    rows: list[GroupRow] = []
    for label in order:
        bucket = buckets[label]
        cells: list[str] = []
        for fn, f in mfields:
            nums = [n for r in bucket if (n := _as_number(r.get(f.key))) is not None]
            cells.append(_format_measure(fn, f, nums))
        rows.append(GroupRow(label=label, count=len(bucket), measures=cells))

    # Sort groups: honor a sort on the group field, else most rows first.
    sort = definition.get("sort", [])
    if sort and gfield and sort[0]["field"] == gfield.key:
        rows.sort(key=lambda g: g.label.lower(), reverse=sort[0]["dir"] == "desc")
    else:
        rows.sort(key=lambda g: (-g.count, g.label.lower()))
    result.group_rows = rows


def _format_measure(fn: str, field: reg.ReportField, nums: list[float]) -> str:
    if not nums:
        return "—"
    if fn == "avg":
        val = sum(nums) / len(nums)
    elif fn == "sum":
        val = sum(nums)
    elif fn == "min":
        val = min(nums)
    elif fn == "max":
        val = max(nums)
    else:
        return "—"
    text = str(int(val)) if float(val).is_integer() else str(round(val, 1))
    return f"{text}%" if field.key.endswith("_pct") else text
