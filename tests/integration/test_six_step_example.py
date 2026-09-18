"""Six-step integration example: the canonical Mirror Memory flow.

This test exercises the full observe → wait → recall → correct → explain → forget
pipeline as described in execution-guide.md §M1 step 3.

It validates that the application service layer works end-to-end.
"""

from datetime import UTC, datetime, timedelta

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
    MemoryType,
    ObserveInput,
    RecallInput,
    RecallOutcome,
    Scope,
    SourceEvent,
    SourceRole,
    SyncAuthorizationInput,
)


def _make_service(session: Session) -> MirrorMemoryService:
    return MirrorMemoryService(session)


def _auth(scope: Scope, version: int = 1) -> AuthorizationSnapshot:
    now = datetime.now(UTC)
    return AuthorizationSnapshot(
        scope=scope,
        purpose="memory_management",
        allowed_operations=["observe", "recall", "correct", "forget", "explain", "export"],
        version=version,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        issuer="test_provider",
    )


class TestSixStepExample:
    """Full lifecycle: observe → recall → correct → explain → forget."""

    def test_full_flow(self, db: Session):
        """User U1 expresses a technical preference, gets recalled, corrects, explains, forgets."""
        svc = _make_service(db)

        scope = Scope(tenant_id="tenant_alpha", app_id="psych", subject_id="user_u1")
        context = MemoryContext(scope=scope, purpose="memory_management", session_id="session_s1")

        # Step 0: Authorize
        auth = _auth(scope)
        result = svc.sync_authorization(SyncAuthorizationInput(
            event_type="grant",
            snapshot=auth,
        ))
        assert result.success

        # Step 1: Observe — user expresses preference
        result = svc.observe(ObserveInput(
            context=context,
            source_event=SourceEvent(
                source_event_id="evt_001",
                source_role=SourceRole.USER,
                text="技术问题请展开解释，我喜欢详细的回答",
                occurred_at=datetime.now(UTC),
                session_id="session_s1",
            ),
        ))
        assert result.success
        assert result.outcome.accepted
        operation_id = result.outcome.operation_id

        # Step 2: Check operation status (simulate processing)
        result = svc.get_operation(GetOperationInput(
            context=context,
            operation_id=operation_id,
        ))
        assert result.success
        assert result.outcome.state.value == "pending"

        # Manually simulate worker processing: create memory atom
        from mirror_memory.adapters.postgresql.repositories import (
            JobRepository,
            MemoryAtomRepository,
        )
        atom_repo = MemoryAtomRepository(db)
        job_repo = JobRepository(db)

        # Acquire lease and complete job
        acquired, token = job_repo.try_acquire_lease(operation_id, "worker_1")
        assert acquired

        # Create the memory atom (simulating extraction)
        atom = atom_repo.create(
            scope=scope,
            type="preference",
            content="技术问题偏好详细解释",
            source_kind="user",
        )
        job_repo.complete(operation_id, token, reason="extracted")

        # Step 3: Recall — new session queries preference
        context2 = MemoryContext(scope=scope, purpose="memory_management", session_id="session_s2")
        result = svc.recall(RecallInput(
            context=context2,
            query="技术回答偏好",
            memory_type=MemoryType.PREFERENCE,
        ))
        assert result.success
        assert result.outcome.outcome == RecallOutcome.FOUND
        assert len(result.outcome.items) >= 1
        assert "详细" in result.outcome.items[0].content or "技术" in result.outcome.items[0].content

        # Step 4: Correct — user changes preference
        result = svc.correct(CorrectInput(
            context=context,
            target_memory_id=atom.id,
            expected_revision=atom.revision,
            correction_text="以后技术问题也先简短回答，我需要再追问",
            user_correction_event_id="evt_002",
        ))
        assert result.success
        assert result.outcome.old_blocked

        # Verify old preference no longer returned in current recall
        result = svc.recall(RecallInput(
            context=context2,
            query="技术回答偏好",
        ))
        # Should return no_memory since the old atom is corrected
        assert result.success
        assert result.outcome.outcome == RecallOutcome.NO_MEMORY

        # Step 5: Explain — trace the source
        result = svc.explain(ExplainInput(
            context=context,
            memory_id=atom.id,
        ))
        assert result.success
        assert result.outcome.memory_id == atom.id

        # Step 6: Forget — user revokes memory
        result = svc.forget(ForgetInput(
            context=context,
            selector=ForgetSelector(memory_ids=[atom.id]),
            request_id="req_forget_001",
        ))
        assert result.success
        assert result.outcome.status.value == "verified"

        # Verify deleted
        result = svc.recall(RecallInput(
            context=context2,
            query="技术回答偏好",
        ))
        assert result.success
        assert result.outcome.outcome == RecallOutcome.NO_MEMORY

    def test_cross_user_isolation(self, db: Session):
        """U1 and U2 don't share memories."""
        svc = _make_service(db)

        scope1 = Scope(tenant_id="t1", app_id="psych", subject_id="u1")
        scope2 = Scope(tenant_id="t1", app_id="psych", subject_id="u2")

        # Authorize both
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=_auth(scope1)))
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=_auth(scope2)))

        ctx1 = MemoryContext(scope=scope1, purpose="memory_management")
        ctx2 = MemoryContext(scope=scope2, purpose="memory_management")

        # U1 observes
        svc.observe(ObserveInput(
            context=ctx1,
            source_event=SourceEvent(
                source_event_id="evt_u1_001",
                source_role=SourceRole.USER,
                text="I love cats",
                occurred_at=datetime.now(UTC),
            ),
        ))

        # U2 should not see U1's data
        result = svc.recall(RecallInput(context=ctx2, query="cats"))
        assert result.success
        assert result.outcome.outcome in (RecallOutcome.NO_MEMORY, RecallOutcome.PENDING)

    def test_revoke_stops_operations(self, db: Session):
        """After revocation, new operations are denied."""
        svc = _make_service(db)

        scope = Scope(tenant_id="t1", app_id="psych", subject_id="u1")
        ctx = MemoryContext(scope=scope, purpose="memory_management")

        # Authorize
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=_auth(scope)))

        # Revoke
        revoke_auth = _auth(scope, version=2)
        revoke_auth.allowed_operations = []
        svc.sync_authorization(SyncAuthorizationInput(event_type="revoke", snapshot=revoke_auth))

        # New observe should be denied
        result = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id="evt_after_revoke",
                source_role=SourceRole.USER,
                text="should not be accepted",
                occurred_at=datetime.now(UTC),
            ),
        ))
        assert not result.success
        assert result.reason_code.value == "AUTH_DENIED"