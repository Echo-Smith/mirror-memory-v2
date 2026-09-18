"""C20: Cross-product reuse.

References: R15, design.md §7 (App1 and next product).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from mirror_memory.application.memory_service import MirrorMemoryService
from mirror_memory.core.types import (
    AuthorizationSnapshot,
    MemoryContext,
    ObserveInput,
    RecallInput,
    RecallOutcome,
    Scope,
    SourceEvent,
    SourceRole,
    SyncAuthorizationInput,
)


@pytest.mark.contract_C20
class TestCrossProductReuse:
    """Verify second product can integrate without App1-specific dependencies."""

    def _auth(self, scope: Scope) -> AuthorizationSnapshot:
        now = datetime.now(UTC)
        return AuthorizationSnapshot(
            scope=scope, purpose="memory_management",
            allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
            version=1, issued_at=now, expires_at=now + timedelta(hours=1), issuer="test",
        )

    def test_independent_app_isolation(self, db: Session):
        """Second product's data is fully isolated from App1."""
        svc = MirrorMemoryService(db)

        app1_scope = Scope(tenant_id="t1", app_id="app_alpha", subject_id="u1")
        app2_scope = Scope(tenant_id="t1", app_id="app_beta", subject_id="u1")

        # Authorize both
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=self._auth(app1_scope)))
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=self._auth(app2_scope)))

        app1_ctx = MemoryContext(scope=app1_scope, purpose="memory_management")
        app2_ctx = MemoryContext(scope=app2_scope, purpose="memory_management")

        # App1 user observes
        svc.observe(ObserveInput(
            context=app1_ctx,
            source_event=SourceEvent(
                source_event_id="app1_001", source_role=SourceRole.USER,
                text="I love Python", occurred_at=datetime.now(UTC),
            ),
        ))

        # App2 user should not see App1 data
        r = svc.recall(RecallInput(context=app2_ctx, query="Python"))
        assert r.success
        assert r.outcome.outcome in (RecallOutcome.NO_MEMORY, RecallOutcome.PENDING)

    def test_app1_regression_after_second_product(self, db: Session):
        """Adding second product doesn't break App1 behavior."""
        svc = MirrorMemoryService(db)

        app1_scope = Scope(tenant_id="t1", app_id="app_alpha", subject_id="u1")
        app2_scope = Scope(tenant_id="t1", app_id="app_beta", subject_id="u1")

        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=self._auth(app1_scope)))
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=self._auth(app2_scope)))

        app1_ctx = MemoryContext(scope=app1_scope, purpose="memory_management")
        now = datetime.now(UTC)

        # App1 works normally
        r = svc.observe(ObserveInput(
            context=app1_ctx,
            source_event=SourceEvent(
                source_event_id="app1_reg_001", source_role=SourceRole.USER,
                text="I like dark mode", occurred_at=now,
            ),
        ))
        assert r.success

        # App2 also works
        app2_ctx = MemoryContext(scope=app2_scope, purpose="memory_management")
        r = svc.observe(ObserveInput(
            context=app2_ctx,
            source_event=SourceEvent(
                source_event_id="app2_001", source_role=SourceRole.USER,
                text="我喜欢简洁的风格", occurred_at=now,
            ),
        ))
        assert r.success

        # App1 still works
        r = svc.recall(RecallInput(context=app1_ctx, query="dark mode"))
        assert r.success