"""Phase 3 tests: the user.can() enforcement layer + require_capability.

The load-bearing guarantee: with the dynamic-RBAC master switch OFF (default),
``can()`` reproduces the legacy admin/internal/open gates exactly; flipping it ON
is a no-op for the seeded roles, and only then do custom roles take effect.
"""

from __future__ import annotations

from app.db import get_session_factory
from app.models import AppUser, Role, RoleCapability, UserRole
from app.services import system_config
from app.services.access import region_scoped
from app.services.passwords import hash_password


def _mk_user(db, username, role_key, *, is_admin=False, is_external=False):
    u = AppUser(
        username=username,
        password_hash=hash_password("pw123456"),
        is_admin=is_admin,
        is_external=is_external,
        is_manager=(role_key == "manager"),
    )
    db.add(u)
    db.flush()
    role = db.query(Role).filter_by(key=role_key).one()
    db.add(UserRole(user_id=u.id, role_id=role.id))
    db.commit()
    return u


def _assert_expected(admin, se, ext):
    # admin: everything; SE: internal but not admin surfaces; external: read-only.
    assert admin.can("settings.manage")
    assert admin.can("project.edit")
    assert not se.can("settings.manage")
    assert se.can("project.edit")
    assert se.can("project.view")
    assert not ext.can("project.edit")
    assert ext.can("project.view")
    assert not ext.can("note.view_internal")


def test_can_parity_between_switch_off_and_on(client):
    db = get_session_factory()()
    try:
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        se = _mk_user(db, "se_p", "standard")
        ext = _mk_user(db, "ext_p", "external", is_external=True)

        # Switch OFF (default) -> legacy gates.
        assert not system_config.rbac_dynamic_enabled()
        _assert_expected(admin, se, ext)

        # Flip ON -> identical, because seeded roles reproduce legacy behavior.
        system_config.set_rbac_dynamic_enabled(db, True)
        db.expire_all()
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        se = db.query(AppUser).filter_by(username="se_p").one()
        ext = db.query(AppUser).filter_by(username="ext_p").one()
        assert system_config.rbac_dynamic_enabled()
        _assert_expected(admin, se, ext)
    finally:
        system_config.set_rbac_dynamic_enabled(db, False)
        db.close()


def test_switch_on_custom_role_capabilities(client):
    db = get_session_factory()()
    try:
        role = Role(key="reporter", name="Reporter", sort_order=50)
        db.add(role)
        db.flush()
        db.add(RoleCapability(role_id=role.id, capability_key="report.generate"))
        u = AppUser(username="rep1", password_hash=hash_password("pw123456"))
        db.add(u)
        db.flush()
        db.add(UserRole(user_id=u.id, role_id=role.id))
        db.commit()

        system_config.set_rbac_dynamic_enabled(db, True)
        u = db.query(AppUser).filter_by(username="rep1").one()
        assert u.can("report.generate")
        assert not u.can("project.edit")
        assert not u.can("settings.manage")
        assert not u.is_superuser
        assert u.effective_capabilities() == {"report.generate"}
    finally:
        system_config.set_rbac_dynamic_enabled(db, False)
        db.close()


def test_custom_superuser_role_bypasses_regions(client):
    db = get_session_factory()()
    try:
        role = Role(key="super2", name="Super2", is_superuser=True, sort_order=15)
        db.add(role)
        db.flush()
        u = AppUser(username="su2", password_hash=hash_password("pw123456"))
        db.add(u)
        db.flush()
        db.add(UserRole(user_id=u.id, role_id=role.id))
        db.commit()

        system_config.set_rbac_dynamic_enabled(db, True)
        system_config.set_region_enforcement_enabled(db, True)
        u = db.query(AppUser).filter_by(username="su2").one()
        assert u.is_superuser
        assert u.can("settings.manage")  # superuser short-circuit
        # Superuser bypasses region scope, just like the built-in admin.
        assert region_scoped(u) is False
    finally:
        system_config.set_rbac_dynamic_enabled(db, False)
        system_config.set_region_enforcement_enabled(db, False)
        db.close()


def test_require_capability_blocks_non_admin(client):
    # An SE (no feedback.manage) is bounced from the feedback board...
    db = get_session_factory()()
    try:
        se = AppUser(username="se_fb", password_hash=hash_password("pw123456"))
        db.add(se)
        db.flush()
        std = db.query(Role).filter_by(key="standard").one()
        db.add(UserRole(user_id=se.id, role_id=std.id))
        db.commit()
    finally:
        db.close()

    login = client.post(
        "/api/v1/auth/session/login",
        json={"username": "se_fb", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    resp = client.get("/ui/feedback/manage", follow_redirects=False)
    assert resp.status_code in (302, 303)  # Forbidden -> redirect to dashboard


def test_require_capability_allows_admin(admin_session):
    # ...while an admin (feedback.manage via legacy admin tier) gets through.
    resp = admin_session.get("/ui/feedback/manage")
    assert resp.status_code == 200, resp.text
