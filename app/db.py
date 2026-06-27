"""SQLAlchemy engine, session, and base model setup."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine() -> Engine:
    """Construct a SQLAlchemy engine for the configured SQLite database."""
    settings = get_settings()
    settings.ensure_data_dir()

    engine = create_engine(
        settings.database_url,
        # SQLite-specific: allow use across threads (FastAPI runs in async + threadpool)
        connect_args={"check_same_thread": False},
        # echo=False; flip to True for SQL debugging
        echo=False,
        future=True,
    )

    # Enforce foreign keys and set sane pragmas on every connection
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Wait up to 5s for the write lock instead of failing immediately with
        # "database is locked" when two writes briefly contend (explicit rather
        # than relying on the driver's implicit default).
        cursor.execute("PRAGMA busy_timeout=5000")
        # Make ON DELETE CASCADE fire row triggers too, so the full-text
        # search index is cleaned up when a parent (e.g. a project) is deleted.
        cursor.execute("PRAGMA recursive_triggers=ON")
        cursor.close()

    return engine


def get_engine() -> Engine:
    """Return the lazily-initialized engine."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the lazily-initialized session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a database session and ensure it is closed."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
