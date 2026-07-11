"""The app version has one source of truth (app/__init__.py) that others follow."""

from __future__ import annotations

import re

import app
from app.config import get_settings


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", app.__version__), app.__version__


def test_settings_app_version_follows_source() -> None:
    # The version shown in the UI / API defaults to app.__version__.
    assert get_settings().app_version == app.__version__


def test_packaging_version_follows_source() -> None:
    # pyproject.toml reads the version dynamically from app.__version__, so the
    # installed package metadata must match. (Requires an editable/real install,
    # which the Docker test image provides.)
    from importlib.metadata import PackageNotFoundError, version

    try:
        assert version("poc-tracker") == app.__version__
    except PackageNotFoundError:  # not installed as a package (rare dev setup)
        pass
