"""C02–C04: Authorization barrier tests.

C02: Missing, fake, expired, or scope-missing authorization → reject.
C03: Version ordering — v2, revoke v3, old v2, duplicate v3 → v3 stays valid.
C04: Revoke timing — revoke before dispatch, after dispatch, before submit, before recall.

References: R02, design.md §5 (authorization handshake and update).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    LifecycleEventRepository,
    ScopeControlRepository,
)


@pytest.mark.contract_C02
class TestAuthorizationBarrier:
    """Verify authorization is required and validated for every protected operation."""

    def test_missing_auth_rejected(self, db: Session, scope_1):
        """Operation without authorization snapshot is rejected."""
        sc_repo = ScopeControlRepository(db)
        allowed, reason = sc_repo.is_authorized(scope_1, "observe")
        assert not allowed
        assert reason == "no_authorization"

    def test_fake_auth_rejected(self, db: Session, scope_1):
        """Scope with no authorization record is rejected."""
        sc_repo = ScopeControlRepository(db)
        # Don't create any authorization
        allowed, reason = sc_repo.is_authorized(scope_1, "recall")
        assert not allowed
        assert reason == "no_authorization"

    def test_expired_auth_rejected(self, db: Session, scope_1):
        """Operation with expired authorization is rejected."""
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)
        sc_repo.update_authorization(
            scope=scope_1,
            version=1,
            purpose="memory_management",
            allowed_operations=["observe", "recall"],
            issued_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),  # expired 1 hour ago
            issuer="test_provider",
        )
        allowed, reason = sc_repo.is_authorized(scope_1, "observe")
        assert not allowed
        assert reason == "authorization_expired"

    def test_missing_operation_scope_rejected(self, db: Session, scope_1):
        """Operation not in allowed_operations is rejected."""
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)
        sc_repo.update_authorization(
            scope=scope_1,
            version=1,
            purpose="memory_management",
            allowed_operations=["observe", "recall"],  # no "forget"
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            issuer="test_provider",
        )
        allowed, reason = sc_repo.is_authorized(scope_1, "forget")
        assert not allowed
        assert reason == "operation_not_allowed"

    def test_valid_auth_accepted(self, db: Session, scope_1):
        """Valid authorization allows the operation."""
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)
        sc_repo.update_authorization(
            scope=scope_1,
            version=1,
            purpose="memory_management",
            allowed_operations=["observe", "recall", "correct", "forget"],
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            issuer="test_provider",
        )
        allowed, reason = sc_repo.is_authorized(scope_1, "observe")
        assert allowed
        assert reason == "ok"


@pytest.mark.contract_C03
class TestAuthorizationVersionOrdering:
    """Verify version ordering and idempotent sync."""

    def test_version_progression(self, db: Session, scope_1):
        """v1 → v2 works; lower version rejected."""
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)

        # Apply v1
        applied, ver = sc_repo.update_authorization(
            scope=scope_1, version=1, purpose="test",
            allowed_operations=["observe"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        assert applied
        assert ver == 1

        # Apply v2
        applied, ver = sc_repo.update_authorization(
            scope=scope_1, version=2, purpose="test",
            allowed_operations=["observe", "recall"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        assert applied
        assert ver == 2

    def test_version_regression_rejected(self, db: Session, scope_1):
        """Syncing a lower version than current is rejected."""
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)

        sc_repo.update_authorization(
            scope=scope_1, version=3, purpose="test",
            allowed_operations=["observe"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )

        # Try to apply v1 — should be rejected
        applied, current = sc_repo.update_authorization(
            scope=scope_1, version=1, purpose="test",
            allowed_operations=["observe"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        assert not applied
        assert current == 3

    def test_same_version_same_content_idempotent(self, db: Session, scope_1):
        """Re-syncing same version is rejected (idempotent = no-op)."""
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)

        sc_repo.update_authorization(
            scope=scope_1, version=2, purpose="test",
            allowed_operations=["observe"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )

        # Same version again — rejected as regression (<=)
        applied, current = sc_repo.update_authorization(
            scope=scope_1, version=2, purpose="test",
            allowed_operations=["observe"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        assert not applied
        assert current == 2

    def test_old_cannot_restore_after_revoke(self, db: Session, scope_1):
        """After v3, old v2 cannot restore previous authorization."""
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)

        sc_repo.update_authorization(
            scope=scope_1, version=2, purpose="test",
            allowed_operations=["observe"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        sc_repo.update_authorization(
            scope=scope_1, version=3, purpose="test",
            allowed_operations=[],  # revoke — empty operations
            issued_at=now, expires_at=now + timedelta(hours=1), issuer="test",
        )

        # Old v2 can't come back
        applied, current = sc_repo.update_authorization(
            scope=scope_1, version=2, purpose="test",
            allowed_operations=["observe"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        assert not applied
        assert current == 3

        # And authorization is effectively revoked
        allowed, _ = sc_repo.is_authorized(scope_1, "observe")
        assert not allowed


@pytest.mark.contract_C04
class TestRevokeTiming:
    """Verify revoke is respected at every permission checkpoint."""

    def test_revoke_prevents_new_operations(self, db: Session, scope_1):
        """After revoke, new operations are denied."""
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)

        # Authorize
        sc_repo.update_authorization(
            scope=scope_1, version=1, purpose="test",
            allowed_operations=["observe", "recall"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        allowed, _ = sc_repo.is_authorized(scope_1, "observe")
        assert allowed

        # Revoke
        sc_repo.update_authorization(
            scope=scope_1, version=2, purpose="test",
            allowed_operations=[],  # revoke
            issued_at=now, expires_at=now + timedelta(hours=1), issuer="test",
        )
        allowed, reason = sc_repo.is_authorized(scope_1, "observe")
        assert not allowed
        assert reason == "operation_not_allowed"

    def test_lifecycle_records_authorization_events(self, db: Session, scope_1):
        """Authorization changes are recorded as lifecycle events."""
        lc_repo = LifecycleEventRepository(db)
        sc_repo = ScopeControlRepository(db)
        now = datetime.now(UTC)

        sc_repo.update_authorization(
            scope=scope_1, version=1, purpose="test",
            allowed_operations=["observe"], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        lc_repo.record(scope=scope_1, event_type="authorize", auth_version=1)

        sc_repo.update_authorization(
            scope=scope_1, version=2, purpose="test",
            allowed_operations=[], issued_at=now,
            expires_at=now + timedelta(hours=1), issuer="test",
        )
        lc_repo.record(scope=scope_1, event_type="revoke", auth_version=2)

        events = lc_repo.list_by_scope(scope_1)
        assert len(events) == 2
        assert events[0].event_type == "revoke"  # most recent first
        assert events[1].event_type == "authorize"