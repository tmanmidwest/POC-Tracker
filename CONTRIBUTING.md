# Contributing

Thanks for considering a contribution to the Questlog.

This is a deliberately narrow-scope tool. Contributions that fit the scope and keep the app simple are welcome.

## Scope reminder

Questlog is a **non-production tool** for tracking proof-of-concept (POC) engagements — customers and contacts, projects, use cases, and reporting — exposed through a web UI, a REST API, and an MCP server. Contributions should not:

- Add features that require additional containers (caches, queues, etc.) — keep the "one container, one command, it runs" deployment story
- Turn this into a full CRM or project-management suite (billing, resourcing, time tracking, forecasting)
- Add multi-tenancy

Contributions that **are** in scope:

- New use-case library sets or lookup fields (statuses, priorities, feature types) that match real POC workflows
- Additional auth methods (OIDC providers) commonly required by customers
- Better seed/demo data
- Bug fixes
- Documentation improvements
- Improvements to the REST API or MCP server surface

## Local development setup

Requirements: Python 3.12+, Docker.

```bash
git clone https://github.com/tmanmidwest/POC-Tracker.git
cd POC-Tracker
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Run locally without Docker:

```bash
export POCT_DATA_DIR=./data
python -m app.main
```

Run the test suite:

```bash
pytest
```

Run linting:

```bash
ruff check .
ruff format --check .
```

## Code style

- Python 3.12+ features welcome
- Type hints required on public functions
- `ruff` is the formatter and linter; CI fails on violations
- FastAPI route handlers should be thin — push business logic to service modules
- Database access through SQLAlchemy ORM, not raw SQL (unless there's a specific reason)
- Tests required for new endpoints and auth flows

## Branching and PRs

- Branch from `main`
- Branch naming: `feature/<short-description>` or `fix/<short-description>`
- Commit messages: conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`)
- PRs should include a description of what changed and why
- Update relevant docs in the same PR
- Add or update tests in the same PR

## Adding a new lookup table

1. Add the SQLAlchemy model in `app/models/`
2. Add the Pydantic schemas in `app/schemas/`
3. Add Alembic migration: `alembic revision --autogenerate -m "add foo table"`
4. Add REST endpoints under `app/api/v1/`
5. Add UI routes under `app/ui/` and templates under `app/templates/`
6. Add seed data in `app/services/seed_data.py` if applicable
7. Update `docs/SCHEMA.md` and `docs/API.md`
8. Add tests in `tests/`

## Adding a field to a project or use case

1. Update the SQLAlchemy model in `app/models/` (e.g. `project.py`, `project_use_case.py`) and add an Alembic migration
2. Update the Pydantic schemas in `app/schemas/`
3. Update the relevant form and list templates in `app/templates/`
4. Update `docs/SCHEMA.md` (and `docs/API.md` if the field is exposed on the API)
5. Add tests

## Reporting issues

When filing an issue, include:

- App version (shown in the app sidebar and at `GET /health`; or `docker image inspect ghcr.io/tmanmidwest/poc-tracker | grep -i version`)
- Deployment environment (local Docker, ECS, AKS, etc.)
- Steps to reproduce
- Expected vs actual behavior
- Relevant log output (with secrets redacted)

## License

This is proprietary software owned by the licensor and made available under the
**PolyForm Strict License 1.0.0** (see [LICENSE](LICENSE)) — it is not open source.
By submitting a contribution, you assign all right, title, and interest in that
contribution to the licensor (Trevor Combs), so the project's ownership and license
remain unambiguous.
