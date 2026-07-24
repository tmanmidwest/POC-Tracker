"""Questlog.

Single source of truth for the app version. Bump ``__version__`` here (and only
here) when cutting a release — ``pyproject.toml`` reads it dynamically and
``app.config.Settings.app_version`` defaults to it, so the packaging version, the
/health endpoint, and the version shown in the UI all stay in lockstep. See
docs/RELEASING.md.
"""

__version__ = "1.1.0"
