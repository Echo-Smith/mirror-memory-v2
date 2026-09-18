"""C20: Cross-product reuse.

References: R15, design.md §7 (Psych and next product).
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
    """Verify second product can integrate without Psych-specific dependencies."""

    def _auth(self, scope: Scope) -> AuthorizationSnapshot:
        now = datetime.now(UTC)
        return AuthorizationSnapshot(
            scope=scope, purpose="memory_management",
            allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
            version=1, issued_at=now, expires_at=now + timedelta(hours=1), issuer="test",
        )

    def test_independent_app_isolation(self, db: Session):
        """Second product's data is fully isolated from Psych."""
        svc = MirrorMemoryService(db)

        psych_scope = Scope(tenant_id="t1", app_id="psych", subject_id="u1")
        birun_scope = Scope(tenant_id="t1", app_id="bi_run_zhi_tan", subject_id="u1")

        # Authorize both
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=self._auth(psych_scope)))
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=self._auth(birun_scope)))

        psych_ctx = MemoryContext(scope=psych_scope, purpose="memory_management")
        birun_ctx = MemoryContext(scope=birun_scope, purpose="memory_management")

        # Psych user observes
        svc.observe(ObserveInput(
            context=psych_ctx,
            source_event=SourceEvent(
                source_event_id="psych_001", source_role=SourceRole.USER,
                text="I love Python", occurred_at=datetime.now(UTC),
            ),
        ))

        # Bi Run user should not see Psych data
        r = svc.recall(RecallInput(context=birun_ctx, query="Python"))
        assert r.success
        assert r.outcome.outcome in (RecallOutcome.NO_MEMORY, RecallOutcome.PENDING)

    def test_psych_regression_after_second_product(self, db: Session):
        """Adding second product doesn't break Psych behavior."""
        svc = MirrorMemoryService(db)

        psych_scope = Scope(tenant_id="t1", app_id="psych", subject_id="u1")
        birun_scope = Scope(tenant_id="t1", app_id="bi_run_zhi_tan", subject_id="u1")

        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=self._auth(psych_scope)))
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=self._auth(birun_scope)))

        psych_ctx = MemoryContext(scope=psych_scope, purpose="memory_management")
        now = datetime.now(UTC)

        # Psych works normally
        r = svc.observe(ObserveInput(
            context=psych_ctx,
            source_event=SourceEvent(
                source_event_id="psych_reg_001", source_role=SourceRole.USER,
                text="I like dark mode", occurred_at=now,
            ),
        ))
        assert r.success

        # Bi Run also works
        birun_ctx = MemoryContext(scope=birun_scope, purpose="memory_management")
        r = svc.observe(ObserveInput(
            context=birun_ctx,
            source_event=SourceEvent(
                source_event_id="birun_001", source_role=SourceRole.USER,
                text="我喜欢简洁的风格", occurred_at=now,
            ),
        ))
        assert r.success

        # Psych still works
        r = svc.recall(RecallInput(context=psych_ctx, query="dark mode"))
        assert r.success