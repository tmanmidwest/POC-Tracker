# Contributing

Thanks for considering a contribution to the POC Tracker.

This is a POC tool with a deliberately narrow scope. Contributions that fit the scope and keep the app simple are welcome.

## Scope reminder

This app is for **non-production POC use**, primarily for testing Saviynt Identity Cloud and similar IGA/IAM REST integrations. Contributions should not:

- Add features that require additional containers (databases, caches, queues)
- Add features that turn this into a production HR system (payroll, time tracking, performance reviews)
- Add multi-tenancy
- Compromise the "one container, one command, it runs" deployment story

Contributions that **are** in scope:

- New lookup fields or employee attributes that match real HR systems
- Additional auth methods commonly required by IGA platforms
- Better seed data
- Bug fixes
- Documentation improvements
- Examples of connector configurations for other IGA products (SailPoint, Okta, Microsoft Entra, etc.)

## Local development setup

Requirements: Python 3.12+, Docker.

```bash
git clone https://github.com/tmanmidwest/hrDemoWebApp.git
cd hrDemoWebApp
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
5. Add UI page under `app/ui/`
6. Add seed data in `app/seed_data.py` if applicable
7. Update `docs/SCHEMA.md` and `docs/API.md`
8. Add tests in `tests/`

## Adding a new employee field

1. Update the `Employee` model and add Alembic migration
2. Update Pydantic schemas
3. Update the employee form template
4. Update the employee list column options
5. Update `docs/SCHEMA.md`
6. Add to the suggested field mapping in `docs/SAVIYNT_INTEGRATION.md`
7. Add tests

## Reporting issues

When filing an issue, include:

- App version (`docker image inspect ghcr.io/tmanmidwest/hrdemowebapp | grep -i version`)
- Deployment environment (local Docker, ECS, AKS, etc.)
- Steps to reproduce
- Expected vs actual behavior
- Relevant log output (with secrets redacted)

## License

By contributing, you agree that your contributions will be licensed under the MIT License (see [LICENSE](LICENSE)).
