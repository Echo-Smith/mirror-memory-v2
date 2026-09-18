"""C19: Developer onboarding walkthrough.

References: R14, execution-guide.md §M1 step 3.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from mirror_memory.application.memory_service import MirrorMemoryService
from mirror_memory.core.types import (
    AuthorizationSnapshot,
    CorrectInput,
    ExplainInput,
    ForgetInput,
    ForgetSelector,
    GetOperationInput,
    MemoryContext,
    ObserveInput,
    RecallInput,
    RecallOutcome,
    Scope,
    SourceEvent,
    SourceRole,
    SyncAuthorizationInput,
)


@pytest.mark.contract_C19
class TestDeveloperOnboarding:
    """Verify a new developer can complete the full flow from docs alone."""

    def test_six_step_example_from_docs(self, db: Session):
        """Full observe → wait → recall → correct → explain → forget succeeds from docs."""
        svc = MirrorMemoryService(db)
        scope = Scope(tenant_id="dev_tenant", app_id="app_alpha", subject_id="dev_user")
        ctx = MemoryContext(scope=scope, purpose="memory_management", session_id="dev_session")

        now = datetime.now(UTC)

        # 1. Authorize
        auth = AuthorizationSnapshot(
            scope=scope, purpose="memory_management",
            allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
            version=1, issued_at=now, expires_at=now + timedelta(hours=1), issuer="dev_test",
        )
        r = svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=auth))
        assert r.success, f"Authorize failed: {r.reason}"

        # 2. Observe
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id="dev_evt_001", source_role=SourceRole.USER,
                text="I prefer detailed technical explanations",
                occurred_at=now, session_id="dev_session",
            ),
        ))
        assert r.success, f"Observe failed: {r.reason}"
        op_id = r.outcome.operation_id

        # 3. Check status
        r = svc.get_operation(GetOperationInput(context=ctx, operation_id=op_id))
        assert r.success

        # 4. Simulate processing (create atom manually)
        from mirror_memory.adapters.postgresql.repositories import (
            JobRepository,
            MemoryAtomRepository,
        )
        job_repo = JobRepository(db)
        atom_repo = MemoryAtomRepository(db)
        acquired, token = job_repo.try_acquire_lease(op_id, "dev_worker")
        assert acquired
        atom = atom_repo.create(
            scope=scope, type="preference",
            content="User prefers detailed technical explanations",
            source_kind="user",
        )
        job_repo.complete(op_id, token, "extracted")

        # 5. Recall
        ctx2 = MemoryContext(scope=scope, purpose="memory_management", session_id="dev_session_2")
        r = svc.recall(RecallInput(context=ctx2, query="technical preference"))
        assert r.success
        assert r.outcome.outcome == RecallOutcome.FOUND
        assert len(r.outcome.items) >= 1

        # 6. Correct
        r = svc.correct(CorrectInput(
            context=ctx, target_memory_id=atom.id, expected_revision=atom.revision,
            correction_text="Brief answers first, I'll ask for details",
            user_correction_event_id="dev_evt_002",
        ))
        assert r.success
        assert r.outcome.old_blocked

        # 7. Explain
        r = svc.explain(ExplainInput(context=ctx, memory_id=atom.id))
        assert r.success
        assert r.outcome.memory_id == atom.id

        # 8. Forget
        r = svc.forget(ForgetInput(
            context=ctx,
            selector=ForgetSelector(memory_ids=[atom.id]),
            request_id="dev_forget_001",
        ))
        assert r.success

        # 9. Verify deleted
        r = svc.recall(RecallInput(context=ctx2, query="technical preference"))
        assert r.success
        assert r.outcome.outcome == RecallOutcome.NO_MEMORY

    def test_failure_hints_actionable(self, db: Session):
        """Error messages point to specific fix actions."""
        svc = MirrorMemoryService(db)
        scope = Scope(tenant_id="hint_tenant", app_id="app_alpha", subject_id="hint_user")
        ctx = MemoryContext(scope=scope, purpose="memory_management")

        # Observe without authorization → clear error
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id="hint_001", source_role=SourceRole.USER,
                text="test", occurred_at=datetime.now(UTC),
            ),
        ))
        assert not r.success
        assert r.reason_code is not None
        assert "auth" in r.reason.lower() or "authorization" in r.reason.lower()

        # Empty selector → clear error
        from datetime import timedelta
        now = datetime.now(UTC)
        auth = AuthorizationSnapshot(
            scope=scope, purpose="memory_management",
            allowed_operations=["forget"],
            version=1, issued_at=now, expires_at=now + timedelta(hours=1), issuer="test",
        )
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=auth))

        r = svc.forget(ForgetInput(
            context=ctx,
            selector=ForgetSelector(),
            request_id="hint_forget",
        ))
        assert not r.success
        assert "selector" in r.reason.lower() or "empty" in r.reason.lower()