"""Concrete MemoryService implementation.

Orchestrates repositories, authorization checks, and domain logic
into the 8 operations defined in the interface contract.
"""

from __future__ import annotations

from datetime import UTC

from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    DeletionJobRepository,
    EvidenceRepository,
    JobRepository,
    LifecycleEventRepository,
    MemoryAtomRepository,
    ReceiptRepository,
    ScopeControlRepository,
    ViewHeadRepository,
)
from mirror_memory.application.budget_service import BudgetService
from mirror_memory.application.service import MemoryService
from mirror_memory.core.types import (
    CorrectInput,
    CorrectOutput,
    DeletionStatus,
    ErrorCode,
    ExplainInput,
    ExplainOutput,
    ExportInput,
    ExportOutput,
    ForgetInput,
    ForgetOutput,
    GetOperationInput,
    GetOperationOutput,
    MemoryType,
    ObserveInput,
    ObserveOutput,
    ProcessingState,
    RecallInput,
    RecallItem,
    RecallOutcome,
    RecallOutput,
    ResultEnvelope,
    SourceRole,
    SyncAuthorizationInput,
    SyncAuthorizationOutput,
)
from mirror_memory.core.utils import utcnow as _now


class MirrorMemoryService(MemoryService):
    """Production implementation of Mirror Memory operations.

    Each method:
    1. Validates authorization
    2. Performs the operation within a transaction
    3. Returns a unified ResultEnvelope
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._scope_repo = ScopeControlRepository(session)
        self._ev_repo = EvidenceRepository(session)
        self._job_repo = JobRepository(session)
        self._atom_repo = MemoryAtomRepository(session)
        self._vh_repo = ViewHeadRepository(session)
        self._receipt_repo = ReceiptRepository(session)
        self._dj_repo = DeletionJobRepository(session)
        self._lc_repo = LifecycleEventRepository(session)
        self._budget_svc = BudgetService(session)
    def _check_auth(self, context, operation: str) -> ResultEnvelope | None:
        """Check authorization. Returns error envelope if denied, None if ok."""
        allowed, reason = self._scope_repo.is_authorized(context.scope, operation, purpose=context.purpose)
        if not allowed:
            code = {
                "no_authorization": ErrorCode.AUTH_DENIED,
                "authorization_expired": ErrorCode.AUTH_EXPIRED,
                "operation_not_allowed": ErrorCode.AUTH_DENIED,
                "scope_deleted": ErrorCode.AUTH_DENIED,
                "purpose_mismatch": ErrorCode.AUTH_DENIED,
            }.get(reason, ErrorCode.AUTH_DENIED)
            return ResultEnvelope(
                success=False,
                reason_code=code,
                reason=f"Authorization failed: {reason}",
            )
        return None

    def observe(self, inp: ObserveInput) -> ResultEnvelope:
        """Accept a user event for processing.

        Flow:
        1. Check authorization
        2. Check deletion barrier
        3. Idempotency check
        4. Create evidence + job in same transaction
        """
        # Auth check
        auth_err = self._check_auth(inp.context, "observe")
        if auth_err:
            return auth_err

        scope = inp.context.scope
        se = inp.source_event

        # Budget check
        budget_result = self._budget_svc.check_and_reserve(
            scope, "call", 1.0, operation="observe",
        )
        if not budget_result.allowed:
            return ResultEnvelope(
                success=False,
                reason_code=ErrorCode.CAPACITY_LIMIT,
                reason=budget_result.reason,
            )

        # Deletion barrier
        if self._scope_repo.is_deleted(scope):
            return ResultEnvelope(
                success=False,
                reason_code=ErrorCode.AUTH_DENIED,
                reason="Scope has been deleted",
            )

        # Idempotency
        status, _existing = self._ev_repo.check_idempotency(
            scope, se.source_event_id, se.text
        )
        if status == "idempotent":
            # Find existing job
            evs = self._ev_repo.list_by_scope(scope)
            for ev in evs:
                if ev.source_event_id == se.source_event_id:
                    jobs = self._job_repo.find_pending(scope)
                    # Return existing operation
                    return ResultEnvelope(
                        success=True,
                        outcome=ObserveOutput(
                            operation_id=jobs[0].id if jobs else ev.id,
                            accepted=True,
                            reason="idempotent",
                        ),
                    )
        if status == "conflict":
            return ResultEnvelope(
                success=False,
                reason_code=ErrorCode.IDEMPOTENCY_CONFLICT,
                reason="Same source_event_id with different content",
            )

        # Create evidence + job atomically
        ev = self._ev_repo.create(
            scope=scope,
            source_event_id=se.source_event_id,
            source_role=se.source_role.value,
            text=se.text,
            occurred_at=se.occurred_at,
            session_id=se.session_id,
            retention_profile=se.retention_profile,
        )
        # Freeze authorization snapshot on the Job
        sc = self._scope_repo.get(scope)
        job = self._job_repo.create(
            scope=scope,
            evidence_id=ev.id,
            purpose=inp.context.purpose,
            accepted_auth_version=sc.auth_version if sc else 0,
            accepted_deletion_generation=sc.deletion_generation if sc else 0,
        )

        # Record receipt
        sc = self._scope_repo.get(scope)
        self._receipt_repo.create(
            scope=scope,
            operation_type="observe",
            auth_version=sc.auth_version if sc else 0,
            target_ids=[ev.id],
        )

        return ResultEnvelope(
            success=True,
            outcome=ObserveOutput(
                operation_id=job.id,
                accepted=True,
            ),
        )

    def get_operation(self, inp: GetOperationInput) -> ResultEnvelope:
        """Query processing job status."""
        auth_err = self._check_auth(inp.context, "observe")
        if auth_err:
            return auth_err

        job = self._job_repo.get_by_id(inp.operation_id)
        if job is None:
            return ResultEnvelope(
                success=False,
                reason="Operation not found",
            )

        # Verify scope ownership
        sc = self._scope_repo.get(inp.context.scope)
        if sc is None or job.scope_id != sc.id:
            return ResultEnvelope(
                success=False,
                reason_code=ErrorCode.SCOPE_INVALID,
                reason="Operation not found in this scope",
            )

        return ResultEnvelope(
            success=True,
            outcome=GetOperationOutput(
                state=ProcessingState(job.state),
                reason=job.reason,
            ),
        )

    def recall(self, inp: RecallInput) -> ResultEnvelope:
        """Retrieve current or historical memories."""
        auth_err = self._check_auth(inp.context, "recall")
        if auth_err:
            return auth_err

        scope = inp.context.scope

        # Check if there are pending jobs
        pending_jobs = self._job_repo.find_pending(scope)
        if pending_jobs and not inp.after_operation_id:
            return ResultEnvelope(
                success=True,
                outcome=RecallOutput(outcome=RecallOutcome.PENDING),
            )

        # Get atoms based on mode
        if inp.mode == "history":
            atoms = self._atom_repo.get_history(scope, inp.memory_type)
        else:
            atoms = self._atom_repo.get_current(scope, inp.memory_type)

        # Filter expired items in current mode
        now = _now()
        items = []
        for atom in atoms:
            if inp.mode == "current" and atom.valid_until:
                valid_until = atom.valid_until
                if valid_until.tzinfo is None:
                    valid_until = valid_until.replace(tzinfo=UTC)
                if valid_until < now:
                    continue

            items.append(RecallItem(
                memory_id=atom.id,
                revision=atom.revision,
                type=MemoryType(atom.type),
                content=atom.content,
                source_kind=SourceRole(atom.source_kind),
                valid_from=atom.valid_from,
                valid_until=atom.valid_until,
                evidence_refs=[atom.source_evidence_id] if atom.source_evidence_id else [],
                selection_reason=atom.selection_reason,
            ))

            # Token budget (simplified: count characters)
            total_chars = sum(len(it.content) for it in items)
            if total_chars > inp.token_budget:
                items.pop()
                break

        if not items:
            return ResultEnvelope(
                success=True,
                outcome=RecallOutput(outcome=RecallOutcome.NO_MEMORY),
            )

        # Create receipt
        sc = self._scope_repo.get(scope)
        receipt = self._receipt_repo.create(
            scope=scope,
            operation_type="recall",
            auth_version=sc.auth_version if sc else 0,
            target_ids=[item.memory_id for item in items],
        )

        vh_rev = self._vh_repo.get_revision(scope)

        return ResultEnvelope(
            success=True,
            outcome=RecallOutput(
                outcome=RecallOutcome.FOUND,
                items=items,
                receipt_id=receipt.id,
                view_revision=vh_rev,
                freshness=now,
            ),
        )

    def correct(self, inp: CorrectInput) -> ResultEnvelope:
        """Mark a specific memory target as corrected by the user."""
        auth_err = self._check_auth(inp.context, "correct")
        if auth_err:
            return auth_err

        scope = inp.context.scope

        # Verify the target exists and belongs to this scope
        atom = self._atom_repo.get_by_id(inp.target_memory_id)
        if atom is None:
            return ResultEnvelope(
                success=False,
                reason="Memory target not found",
            )

        sc = self._scope_repo.get(scope)
        if sc is None or atom.scope_id != sc.id:
            return ResultEnvelope(
                success=False,
                reason_code=ErrorCode.SCOPE_INVALID,
                reason="Memory target not in this scope",
            )

        # Attempt correction
        success, actual_rev = self._atom_repo.mark_corrected(
            inp.target_memory_id, inp.expected_revision
        )
        if not success:
            return ResultEnvelope(
                success=False,
                reason_code=ErrorCode.REVISION_CONFLICT,
                reason=f"Revision conflict: expected {inp.expected_revision}, actual {actual_rev}",
            )

        # Record receipt
        receipt = self._receipt_repo.create(
            scope=scope,
            operation_type="correct",
            auth_version=sc.auth_version if sc else 0,
            target_ids=[inp.target_memory_id],
            detail={"correction_text": inp.correction_text},
        )

        # Record lifecycle event
        self._lc_repo.record(
            scope=scope,
            event_type="correct",
            auth_version=sc.auth_version if sc else 0,
            detail={"target": inp.target_memory_id, "old_revision": inp.expected_revision},
        )

        return ResultEnvelope(
            success=True,
            outcome=CorrectOutput(
                old_blocked=True,
                receipt_id=receipt.id,
            ),
        )

    def forget(self, inp: ForgetInput) -> ResultEnvelope:
        """Delete memories matching an explicit selector."""
        auth_err = self._check_auth(inp.context, "forget")
        if auth_err:
            return auth_err

        scope = inp.context.scope
        sel = inp.selector

        # Validate selector — empty is rejected
        if not sel.scope_wide and not sel.memory_ids and not sel.evidence_ids:
            return ResultEnvelope(
                success=False,
                reason="Empty selector: must specify memory_ids, evidence_ids, or scope_wide=True",
            )

        # Check for existing deletion request (idempotency)
        existing = self._dj_repo.get_by_request_id(scope, inp.request_id)
        if existing:
            return ResultEnvelope(
                success=True,
                outcome=ForgetOutput(
                    status=DeletionStatus(existing.status),
                    receipt_id=existing.id,
                    reason="idempotent",
                ),
            )

        # Create deletion job
        dj = self._dj_repo.create(
            scope=scope,
            request_id=inp.request_id,
            selector=sel.model_dump(),
        )

        sc = self._scope_repo.get_or_create(scope)

        if sel.scope_wide:
            # Scope-wide: increment generation barrier, delete all
            gen = self._scope_repo.mark_deleted(scope)
            count = self._atom_repo.soft_delete_by_scope(scope, gen)
        elif sel.memory_ids:
            # Targeted: only mark specific atoms, do NOT bump scope generation
            gen = sc.deletion_generation + 1
            count = self._atom_repo.soft_delete_by_ids(scope, sel.memory_ids, gen)
        elif sel.evidence_ids:
            # Evidence-level: delete evidence and derived atoms
            gen = sc.deletion_generation + 1
            atoms_deleted, evidence_deleted = self._atom_repo.soft_delete_by_evidence_ids(
                scope, sel.evidence_ids, gen
            )
            count = atoms_deleted + evidence_deleted
        else:
            count = 0

        # Verify deletion actually took effect
        remaining = self._atom_repo.get_current(scope)
        if sel.scope_wide and len(remaining) > 0:
            status = DeletionStatus.FAILED
            reason = f"Scope delete incomplete: {len(remaining)} remain"
            self._dj_repo.update_status(dj.id, "failed", reason)
        elif count == 0 and (sel.memory_ids or sel.evidence_ids):
            status = DeletionStatus.FAILED
            reason = "No matching items found to delete"
            self._dj_repo.update_status(dj.id, "failed", reason)
        else:
            status = DeletionStatus.VERIFIED
            reason = f"Deleted {count} items"
            self._dj_repo.update_status(dj.id, "verified", reason)

        # Record lifecycle
        sc = self._scope_repo.get(scope) or sc
        self._lc_repo.record(
            scope=scope,
            event_type="delete",
            auth_version=sc.auth_version if sc else 0,
            detail={"generation": gen, "count": count, "status": status.value},
        )

        return ResultEnvelope(
            success=status == DeletionStatus.VERIFIED,
            outcome=ForgetOutput(
                status=status,
                receipt_id=dj.id,
                reason=reason,
            ),
        )

    def explain(self, inp: ExplainInput) -> ResultEnvelope:
        """Get source chain and reasoning behind a memory."""
        auth_err = self._check_auth(inp.context, "explain")
        if auth_err:
            return auth_err

        scope = inp.context.scope

        if inp.memory_id:
            atom = self._atom_repo.get_by_id(inp.memory_id)
            if atom is None:
                return ResultEnvelope(
                    success=False,
                    reason="Memory not found",
                )

            sc = self._scope_repo.get(scope)
            if sc is None or atom.scope_id != sc.id:
                return ResultEnvelope(
                    success=False,
                    reason_code=ErrorCode.SCOPE_INVALID,
                    reason="Memory not in this scope",
                )

            # Build source chain
            source_chain = []
            if atom.source_evidence_id:
                ev = self._ev_repo.get_by_id(atom.source_evidence_id)
                if ev:
                    source_chain.append({
                        "evidence_id": ev.id,
                        "source_role": ev.source_role,
                        "text_preview": ev.text[:100],
                        "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                    })

            return ResultEnvelope(
                success=True,
                outcome=ExplainOutput(
                    memory_id=atom.id,
                    source_chain=source_chain,
                    revision_history=[{"revision": atom.revision, "is_current": atom.is_current}],
                    authorization_version=sc.auth_version if sc else None,
                    selection_reason=atom.selection_reason,
                ),
            )

        return ResultEnvelope(
            success=False,
            reason="Must specify memory_id or receipt_id",
        )

    def export(self, inp: ExportInput) -> ResultEnvelope:
        """Async export of user data (simplified for M1)."""
        auth_err = self._check_auth(inp.context, "export")
        if auth_err:
            return auth_err

        scope = inp.context.scope

        # Gather metadata only — sensitive content goes in the response, NOT the receipt
        evs = self._ev_repo.list_by_scope(scope)
        atoms = self._atom_repo.get_history(scope)

        sc = self._scope_repo.get(scope)
        receipt = self._receipt_repo.create(
            scope=scope,
            operation_type="export",
            auth_version=sc.auth_version if sc else 0,
            detail={
                "evidence_count": len(evs),
                "memory_count": len(atoms),
                "exported_at": _now().isoformat(),
            },
        )

        return ResultEnvelope(
            success=True,
            outcome=ExportOutput(status="ready", download_url=f"receipt://{receipt.id}"),
        )

    def sync_authorization(self, inp: SyncAuthorizationInput) -> ResultEnvelope:
        """Sync a versioned authorization event from a trusted provider."""
        snap = inp.snapshot
        scope = snap.scope

        if inp.event_type in ("grant", "update"):
            applied, current = self._scope_repo.update_authorization(
                scope=scope,
                version=snap.version,
                purpose=snap.purpose,
                allowed_operations=snap.allowed_operations,
                issued_at=snap.issued_at,
                expires_at=snap.expires_at,
                issuer=snap.issuer,
            )
            if not applied:
                return ResultEnvelope(
                    success=False,
                    reason_code=ErrorCode.REVISION_CONFLICT,
                    reason=f"Version {snap.version} <= current {current}",
                )

            self._lc_repo.record(
                scope=scope,
                event_type="authorize",
                auth_version=snap.version,
            )

            return ResultEnvelope(
                success=True,
                outcome=SyncAuthorizationOutput(applied_version=snap.version, ack=True),
            )

        elif inp.event_type == "revoke":
            # Revoke = set version higher with empty operations
            applied, current = self._scope_repo.update_authorization(
                scope=scope,
                version=snap.version,
                purpose=snap.purpose,
                allowed_operations=[],
                issued_at=snap.issued_at,
                expires_at=snap.expires_at,
                issuer=snap.issuer,
            )
            if not applied:
                return ResultEnvelope(
                    success=False,
                    reason_code=ErrorCode.REVISION_CONFLICT,
                    reason=f"Version {snap.version} <= current {current}",
                )

            # Cancel pending jobs
            pending = self._job_repo.find_pending(scope)
            for job in pending:
                self._job_repo.cancel(job.id)

            self._lc_repo.record(
                scope=scope,
                event_type="revoke",
                auth_version=snap.version,
            )

            return ResultEnvelope(
                success=True,
                outcome=SyncAuthorizationOutput(applied_version=snap.version, ack=True),
            )

        return ResultEnvelope(
            success=False,
            reason=f"Unknown event_type: {inp.event_type}",
        )