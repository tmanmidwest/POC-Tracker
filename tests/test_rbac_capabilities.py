"""Tests for the Phase 1 RBAC capability registry.

Pure registry checks — no DB, no enforcement (those arrive in Phases 2 & 3).
Their job is to keep the code-defined catalog internally consistent so a typo
can't ship a capability that nothing could ever match.
"""

from __future__ import annotations

from app.services import rbac
from app.services.rbac import capabilities as caps


def test_keys_are_unique():
    keys = [c.key for c in caps.CAPABILITIES]
    assert len(keys) == len(set(keys))
    assert caps.CAPABILITY_KEYS == set(keys)


def test_keys_are_resource_action_slugs():
    for cap in caps.CAPABILITIES:
        assert cap.key == cap.key.lower(), cap.key
        assert cap.key.count(".") == 1, cap.key
        resource, action = cap.key.split(".")
        assert resource and action, cap.key


def test_every_capability_has_a_label_and_description():
    for cap in caps.CAPABILITIES:
        assert cap.label.strip(), cap.key
        assert cap.description.strip(), cap.key


def test_every_area_is_declared():
    assert {c.area for c in caps.CAPABILITIES} <= set(caps.AREAS)


def test_get_capability_round_trips():
    for cap in caps.CAPABILITIES:
        assert caps.get_capability(cap.key) is cap
    assert caps.get_capability("does.notexist") is None


def test_is_valid_capability():
    assert caps.is_valid_capability("project.edit")
    assert not caps.is_valid_capability("project.frobnicate")
    assert not caps.is_valid_capability("")


def test_role_manage_capability_exists():
    # The role builder's own gate must be in the catalog (Phase 5/6 depend on it).
    assert caps.is_valid_capability("role.manage")


def test_capabilities_by_area_is_ordered_and_complete():
    grouped = caps.capabilities_by_area()
    # Areas appear in AREAS order (empty areas omitted).
    assert list(grouped) == [a for a in caps.AREAS if a in grouped]
    # Every capability shows up exactly once, under its own area.
    flat = [cap for members in grouped.values() for cap in members]
    assert len(flat) == len(caps.CAPABILITIES)
    for area, members in grouped.items():
        for cap in members:
            assert cap.area == area


def test_package_reexports_public_surface():
    assert rbac.CAPABILITIES is caps.CAPABILITIES
    assert rbac.is_valid_capability("audit.view")
