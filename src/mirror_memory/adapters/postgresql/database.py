"""Database engine, session factory, and environment guard.

Every environment uses an isolated database and credentials.
Test startup MUST verify the environment marker before any destructive operation.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker


def _get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    return url


def _get_env_marker() -> str:
    return os.environ.get("MIRROR_ENV", "unknown")


def _is_safe_test_env() -> bool:
    """Only test environments may run destructive operations (DROP, RESET)."""
    return _get_env_marker() == "test"


class Database:
    """Manages engine and session lifecycle."""

    def __init__(self, database_url: str | None = None) -> None:
        self._url = database_url or _get_database_url()
        self._engine = create_engine(self._url, pool_pre_ping=True)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

    @property
    def engine(self):
        return self._engine

    def session(self) -> Generator[Session, None, None]:
        """Yield a transactional session; auto-commit on success, rollback on error."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def transaction(self) -> Generator[Session, None, None]:
        """Explicit transaction context manager."""
        yield from self.session()

    def reset_schema(self) -> None:
        """Drop and recreate all tables. ONLY allowed in test environments.

        Raises RuntimeError if MIRROR_ENV != 'test'.
        """
        if not _is_safe_test_env():
            raise RuntimeError(
                f"Schema reset is only allowed in test environments. "
                f"Current MIRROR_ENV={_get_env_marker()!r}"
            )
        from mirror_memory.adapters.postgresql.models import Base

        Base.metadata.drop_all(self._engine)
        Base.metadata.create_all(self._engine)

    def check_connection(self) -> bool:
        """Verify database connectivity by attempting to acquire and release a connection."""
        try:
            with self._engine.connect() as _conn:
                pass
            return True
        except (OSError, SQLAlchemyError):
            return False

    def dispose(self) -> None:
        """Release all connection pool resources."""
        self._engine.dispose()