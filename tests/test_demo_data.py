"""Tests for the demo-data seeder and its guarded CLI (app.scripts.seed_demo)."""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient


def test_seed_demo_data_populates_and_is_idempotent(client: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import Customer
    from app.services.demo_data import DEMO_CUSTOMER_NAMES, seed_demo_data

    db = get_session_factory()()
    first = seed_demo_data(db)
    assert first["customers"] == len(DEMO_CUSTOMER_NAMES)
    assert first["projects"] == len(DEMO_CUSTOMER_NAMES)
    assert first["use_cases"] > 0

    names = {c.name for c in db.query(Customer).all()}
    assert set(DEMO_CUSTOMER_NAMES).issubset(names)

    # Re-running skips everything (keyed by customer name).
    second = seed_demo_data(db)
    assert second["customers"] == 0
    assert second["skipped"] == len(DEMO_CUSTOMER_NAMES)


def test_seed_demo_data_creates_at_risk_and_stalled(client: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import Project
    from app.services.demo_data import seed_demo_data
    from app.services.insights import is_at_risk, is_stalled

    db = get_session_factory()()
    seed_demo_data(db)
    projects = db.query(Project).all()

    assert any(is_at_risk(p) for p in projects), "expected an at-risk demo project"
    assert any(is_stalled(p) for p in projects), "expected a stalled demo project"


def test_purge_demo_data_removes_everything(client: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import AppUser, Customer
    from app.services.demo_data import (
        DEMO_CUSTOMER_NAMES,
        DEMO_ENGINEERS,
        purge_demo_data,
        seed_demo_data,
    )

    db = get_session_factory()()
    seed_demo_data(db)
    removed = purge_demo_data(db)

    assert removed["customers"] == len(DEMO_CUSTOMER_NAMES)
    assert removed["engineers"] == len(DEMO_ENGINEERS)

    names = {c.name for c in db.query(Customer).all()}
    assert not set(DEMO_CUSTOMER_NAMES).intersection(names)

    usernames = {u.username for u in db.query(AppUser).all()}
    for username, _display in DEMO_ENGINEERS:
        assert username not in usernames


def test_cli_dry_run_writes_nothing(client, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from app.db import get_session_factory
    from app.models import Customer
    from app.scripts.seed_demo import main
    from app.services.demo_data import DEMO_CUSTOMER_NAMES

    monkeypatch.setattr(sys, "argv", ["poct-seed-demo"])  # no --yes
    assert main() == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Target database:" in out

    db = get_session_factory()()
    names = {c.name for c in db.query(Customer).all()}
    assert not set(DEMO_CUSTOMER_NAMES).intersection(names)


def test_cli_applies_with_yes_then_purges(client, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from app.db import get_session_factory
    from app.models import Customer
    from app.scripts.seed_demo import main
    from app.services.demo_data import DEMO_CUSTOMER_NAMES

    monkeypatch.setattr(sys, "argv", ["poct-seed-demo", "--yes"])
    assert main() == 0
    db = get_session_factory()()
    names = {c.name for c in db.query(Customer).all()}
    assert set(DEMO_CUSTOMER_NAMES).issubset(names)

    monkeypatch.setattr(sys, "argv", ["poct-seed-demo", "--purge", "--yes"])
    assert main() == 0
    db2 = get_session_factory()()
    names2 = {c.name for c in db2.query(Customer).all()}
    assert not set(DEMO_CUSTOMER_NAMES).intersection(names2)


# ---------------------------------------------------------------------------
# Admin UI (Settings → Demo data), gated behind POCT_ENABLE_DEMO_TOOLS
# ---------------------------------------------------------------------------


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def _login_admin(client: TestClient) -> None:
    from app.config import get_settings

    s = get_settings()
    _login(client, s.initial_admin_username, s.initial_admin_password)


def _enable_demo_tools(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import get_settings

    monkeypatch.setenv("POCT_ENABLE_DEMO_TOOLS", "1")
    get_settings.cache_clear()


def _demo_count() -> int:
    from app.db import get_session_factory
    from app.models import Customer
    from app.services.demo_data import DEMO_CUSTOMER_NAMES

    db = get_session_factory()()
    return (
        db.query(Customer)
        .filter(Customer.name.in_(DEMO_CUSTOMER_NAMES))
        .count()
    )


def test_demo_data_page_hidden_and_404_when_disabled(client: TestClient) -> None:
    _login_admin(client)

    # Tile is not on the settings hub...
    hub = client.get("/ui/settings").text
    assert "/ui/settings/demo-data" not in hub

    # ...and the route itself does not exist.
    resp = client.get("/ui/settings/demo-data")
    assert resp.status_code == 404


def test_demo_data_load_and_remove_via_ui(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.services.demo_data import DEMO_CUSTOMER_NAMES

    _enable_demo_tools(monkeypatch)
    _login_admin(client)

    # Tile shows and the page renders.
    assert "/ui/settings/demo-data" in client.get("/ui/settings").text
    assert client.get("/ui/settings/demo-data").status_code == 200

    # Load.
    resp = client.post(
        "/ui/settings/demo-data", data={"action": "load"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert _demo_count() == len(DEMO_CUSTOMER_NAMES)

    # Remove with the wrong confirmation phrase — nothing is deleted.
    resp = client.post(
        "/ui/settings/demo-data",
        data={"action": "remove", "confirm": "nope"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _demo_count() == len(DEMO_CUSTOMER_NAMES)

    # Remove with the correct phrase — demo data is gone.
    resp = client.post(
        "/ui/settings/demo-data",
        data={"action": "remove", "confirm": "REMOVE"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _demo_count() == 0


def test_demo_data_forbidden_for_non_admin(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.db import get_session_factory
    from app.models import AppUser
    from app.services.passwords import hash_password

    _enable_demo_tools(monkeypatch)

    db = get_session_factory()()
    db.add(
        AppUser(
            username="stduser",
            password_hash=hash_password("password123"),
            is_admin=False,
            is_external=False,
            is_active=True,
        )
    )
    db.commit()
    _login(client, "stduser", "password123")

    resp = client.get("/ui/settings/demo-data", follow_redirects=False)
    assert resp.status_code == 303  # _Forbidden → redirect to dashboard
    assert "/ui/dashboard" in resp.headers["location"]
