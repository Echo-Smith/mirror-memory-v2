"""Shared test fixtures for Mirror Memory v2.

Provides:
- Isolated test database (SQLite for dev, PostgreSQL for integration)
- Synthetic MemoryContext and AuthorizationSnapshot factories
- FakeClock for deterministic time control
- FakeModelClient for M1 (no paid model calls)
"""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Environment setup — SQLite for local dev, PostgreSQL when DATABASE_URL is set
os.environ.setdefault("MIRROR_ENV", "test")

# Use SQLite if no DATABASE_URL is provided (development mode)
_dev_db_url = os.environ.get("DATABASE_URL", "sqlite:///./test_mirror_memory.db")
os.environ.setdefault("DATABASE_URL", _dev_db_url)

from mirror_memory.adapters.postgresql.database import Database
from mirror_memory.adapters.postgresql.models import Base
from mirror_memory.core.types import (
    AuthorizationSnapshot,
    MemoryContext,
    Scope,
    SourceEvent,
    SourceRole,
)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


from tests._guard import assert_safe_to_drop

# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def db_url() -> str:
    """Database URL for the test session."""
    return os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def _db_engine(db_url: str):
    """Session-scoped engine — created once, disposed at end."""
    connect_args = {}
    if _is_sqlite(db_url):
        connect_args["check_same_thread"] = False
    engine = create_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    yield engine
    engine.dispose()
    # Clean up SQLite file
    if _is_sqlite(db_url):
        import pathlib

        db_path = db_url.replace("sqlite:///", "")
        pathlib.Path(db_path).unlink(missing_ok=True)


@pytest.fixture()
def db(_db_engine) -> Generator[Session, None, None]:
    """Per-test database session with automatic schema reset.

    Each test gets a clean schema. The session auto-commits on success,
    rolls back on failure.
    """
    # SAFETY GUARD: never drop tables on non-test databases
    assert_safe_to_drop(os.environ.get("MIRROR_ENV", "unknown"), os.environ.get("DATABASE_URL", ""))

    # Reset schema for isolation
    Base.metadata.drop_all(_db_engine)
    Base.metadata.create_all(_db_engine)

    factory = sessionmaker(bind=_db_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def database(_db_engine) -> Database:
    """Database instance using the test engine."""
    db = Database(os.environ["DATABASE_URL"])
    yield db
    db.dispose()


# ---------------------------------------------------------------------------
# Synthetic data factories
# ---------------------------------------------------------------------------

# Fixed tenant/app pairs for deterministic testing
TENANT_1 = "tenant_alpha"
APP_1 = "psych"
SUBJECT_1 = "user_001"

TENANT_2 = "tenant_beta"
APP_2 = "bi_run_zhi_tan"
SUBJECT_2 = "user_002"


@pytest.fixture
def scope_1() -> Scope:
    """Primary test scope."""
    return Scope(tenant_id=TENANT_1, app_id=APP_1, subject_id=SUBJECT_1)


@pytest.fixture
def scope_2() -> Scope:
    """Secondary test scope (different subject, same app)."""
    return Scope(tenant_id=TENANT_1, app_id=APP_1, subject_id=SUBJECT_2)


@pytest.fixture
def scope_cross_app() -> Scope:
    """Cross-app scope for isolation tests."""
    return Scope(tenant_id=TENANT_1, app_id=APP_2, subject_id=SUBJECT_1)


@pytest.fixture
def scope_cross_tenant() -> Scope:
    """Cross-tenant scope for isolation tests."""
    return Scope(tenant_id=TENANT_2, app_id=APP_1, subject_id=SUBJECT_1)


@pytest.fixture
def context_1(scope_1: Scope) -> MemoryContext:
    """Primary test context."""
    return MemoryContext(scope=scope_1, purpose="memory_management", session_id="session_s1")


@pytest.fixture
def context_2(scope_2: Scope) -> MemoryContext:
    """Secondary test context."""
    return MemoryContext(scope=scope_2, purpose="memory_management", session_id="session_s2")


@pytest.fixture
def context_cross_app(scope_cross_app: Scope) -> MemoryContext:
    """Cross-app context for isolation tests."""
    return MemoryContext(scope=scope_cross_app, purpose="memory_management")


@pytest.fixture
def context_cross_tenant(scope_cross_tenant: Scope) -> MemoryContext:
    """Cross-tenant context for isolation tests."""
    return MemoryContext(scope=scope_cross_tenant, purpose="memory_management")


def make_auth_snapshot(
    scope: Scope,
    version: int = 1,
    operations: list[str] | None = None,
    expires_in_minutes: int = 60,
) -> AuthorizationSnapshot:
    """Factory for AuthorizationSnapshot with sensible defaults."""
    now = datetime.now(UTC)
    return AuthorizationSnapshot(
        scope=scope,
        purpose="memory_management",
        allowed_operations=operations or ["observe", "recall", "correct", "forget", "explain", "export"],
        version=version,
        issued_at=now,
        expires_at=now + timedelta(minutes=expires_in_minutes),
        issuer="test_provider",
    )


@pytest.fixture
def auth_1(scope_1: Scope) -> AuthorizationSnapshot:
    """Default authorization for primary scope."""
    return make_auth_snapshot(scope_1)


@pytest.fixture
def auth_2(scope_2: Scope) -> AuthorizationSnapshot:
    """Default authorization for secondary scope."""
    return make_auth_snapshot(scope_2)


def make_source_event(
    event_id: str = "evt_001",
    role: SourceRole = SourceRole.USER,
    text: str = "I prefer detailed technical explanations",
    session_id: str = "session_s1",
) -> SourceEvent:
    """Factory for SourceEvent with sensible defaults."""
    return SourceEvent(
        source_event_id=event_id,
        source_role=role,
        text=text,
        occurred_at=datetime.now(UTC),
        session_id=session_id,
    )


@pytest.fixture
def source_event_1() -> SourceEvent:
    """Default user source event."""
    return make_source_event()


# ---------------------------------------------------------------------------
# FakeClock — deterministic time control
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable clock for deterministic time testing.

    Usage:
        clock = FakeClock()
        now = clock.now()        # returns frozen time
        clock.advance(seconds=5) # move forward 5 seconds
    """

    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> datetime:
        delta = timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        self._now += delta
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt


@pytest.fixture
def clock() -> FakeClock:
    """Deterministic clock for time-dependent tests."""
    return FakeClock()


# ---------------------------------------------------------------------------
# FakeModelClient — M1 deterministic extractor
# ---------------------------------------------------------------------------


class FakeModelClient:
    """Deterministic model client for M1 testing.

    Returns pre-configured extraction results. Does not call any external API.
    Tracks call count and last input for assertion.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.last_input: str | None = None
        self._responses: list[dict] = []
        self._response_index = 0

    def set_responses(self, responses: list[dict]) -> None:
        """Pre-configure extraction results to return in order."""
        self._responses = responses
        self._response_index = 0

    def extract(self, text: str, context: dict | None = None) -> dict:
        """Simulate extraction. Returns next pre-configured response or empty."""
        self.call_count += 1
        self.last_input = text
        if self._response_index < len(self._responses):
            resp = self._responses[self._response_index]
            self._response_index += 1
            return resp
        return {"atoms": [], "confidence": 0.0}

    def is_available(self) -> bool:
        return True


@pytest.fixture
def fake_model() -> FakeModelClient:
    """Deterministic model client for testing."""
    return FakeModelClient()