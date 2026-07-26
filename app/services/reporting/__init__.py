"""In-app reporting: a declarative, region-scoped, savable report layer.

Modules:

- :mod:`registry` — the pure field catalog + definition validation (safety boundary).
- :mod:`engine`   — region-scoped loading + Python filter/group/aggregate + derived
                    ``insights`` values; produces a ``RunResult``.
- :mod:`saved`    — SavedReport CRUD + visibility resolution.
- :mod:`exports`  — RunResult -> CSV / XLSX / PDF / email HTML.
- :mod:`scheduler`— the background sweep that emails due scheduled reports.

See ``docs/reporting-plan.md``.
"""
