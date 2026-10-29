"""Database engine and session helpers."""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def normalize_database_url(url: str) -> str:
    """Normalize common Postgres URLs for SQLAlchemy + psycopg v3."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


@lru_cache
def get_engine() -> Engine:
    """Create (and cache) the SQLAlchemy engine from settings."""
    settings = get_settings()
    url = normalize_database_url(settings.database_url)
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
        future=True,
    )

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ARG001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_session_factory() -> sessionmaker[Session]:
    """Return a session factory bound to the application engine."""
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Dispose and clear the cached engine (useful in tests)."""
    try:
        get_engine().dispose()
    finally:
        get_engine.cache_clear()
