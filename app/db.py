"""SQLAlchemy engine, session, and base model setup."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine() -> Engine:
    """Construct the Postgres engine: a health-checked, sized connection pool.

    ``pool_pre_ping`` transparently recovers connections dropped by the DB or a
    proxy (common with managed Postgres / RDS); ``pool_recycle`` retires idle
    connections before the server closes them.
    """
    settings = get_settings()
    # The signing/session keys live under data_dir (on EFS in AWS), so ensure it
    # exists even though the database itself is remote.
    settings.ensure_data_dir()

    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle_seconds,
        echo=False,
        future=True,
    )


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
