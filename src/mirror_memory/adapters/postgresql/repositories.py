"""Repository implementations for Mirror Memory v2.

Each repository operates within a provided SQLAlchemy Session.
Transactions are managed by the caller (service layer).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.models import (
    DeletionJob,
    Evidence,
    GenerationRun,
    Job,
    LifecycleEvent,
    MemoryAtomModel,
    Receipt,
    ScopeControl,
    ViewHead,
    ViewRevision,
)
from mirror_memory.core.types import Scope


def _scope_key(scope: Scope) -> str:
    return scope.key()


def _hash_text(text: str) -> str:
    """SHA-256 of normalized text for idempotency checks."""
    return hashlib.sha256(text.strip().encode()).hexdigest()


from mirror_memory.core.utils import utcnow as _utcnow

# ---------------------------------------------------------------------------
# Scope Control Repository
# ---------------------------------------------------------------------------


class ScopeControlRepository:
    """Manages per-scope authorization state and deletion barrier."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, scope: Scope) -> ScopeControl:
        """Get existing scope control or create a new one."""
        stmt = select(ScopeControl).where(
            ScopeControl.tenant_id == scope.tenant_id,
            ScopeControl.app_id == scope.app_id,
            ScopeControl.subject_id == scope.subject_id,
        )
        existing = self._session.execute(stmt).scalar_one_or_none()
        if existing:
            return existing
        sc = ScopeControl(
            tenant_id=scope.tenant_id,
            app_id=scope.app_id,
            subject_id=scope.subject_id,
        )
        self._session.add(sc)
        self._session.flush()
        return sc

    def get(self, scope: Scope) -> ScopeControl | None:
        stmt = select(ScopeControl).where(
            ScopeControl.tenant_id == scope.tenant_id,
            ScopeControl.app_id == scope.app_id,
            ScopeControl.subject_id == scope.subject_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def lock_by_id(self, scope_id: str) -> ScopeControl | None:
        """SELECT FOR UPDATE — blocks concurrent revoke/delete until transaction ends."""
        stmt = select(ScopeControl).where(ScopeControl.id == scope_id).with_for_update()
        return self._session.execute(stmt).scalar_one_or_none()

    def update_authorization(
        self,
        scope: Scope,
        version: int,
        purpose: str,
        allowed_operations: list[str],
        issued_at: datetime,
        expires_at: datetime,
        issuer: str,
    ) -> tuple[bool, int]:
        """Update authorization if version is higher. Returns (applied, current_version)."""
        sc = self.get_or_create(scope)
        if version <= sc.auth_version:
            return False, sc.auth_version
        sc.auth_version = version
        sc.auth_purpose = purpose
        sc.auth_allowed_operations = allowed_operations
        sc.auth_issued_at = issued_at
        sc.auth_expires_at = expires_at
        sc.auth_issuer = issuer
        self._session.flush()
        return True, version

    def is_authorized(self, scope: Scope, operation: str, purpose: str | None = None) -> tuple[bool, str]:
        """Check if operation is authorized for scope. Returns (allowed, reason).

        Validates: not deleted, version > 0, not expired, operation in allowed list, purpose matches.
        """
        sc = self.get(scope)
        if sc is None:
            return False, "no_authorization"
        if sc.deleted_at is not None:
            return False, "scope_deleted"
        if sc.auth_version == 0:
            return False, "no_authorization"
        if sc.auth_expires_at:
            expires = sc.auth_expires_at
            # Normalize naive/aware for SQLite compatibility
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires < _utcnow():
                return False, "authorization_expired"
        if operation not in (sc.auth_allowed_operations or []):
            return False, "operation_not_allowed"
        if purpose is not None and sc.auth_purpose and purpose != sc.auth_purpose:
            return False, "purpose_mismatch"
        return True, "ok"

    def is_deleted(self, scope: Scope) -> bool:
        """Check if scope has been deleted."""
        sc = self.get(scope)
        return sc is not None and sc.deleted_at is not None

    def mark_deleted(self, scope: Scope) -> int:
        """Increment deletion generation and mark as deleted. Returns new generation."""
        sc = self.get_or_create(scope)
        sc.deletion_generation += 1
        sc.deleted_at = _utcnow()
        self._session.flush()
        return sc.deletion_generation

    def get_deletion_generation(self, scope: Scope) -> int:
        sc = self.get(scope)
        return sc.deletion_generation if sc else 0


# ---------------------------------------------------------------------------
# Evidence Repository
# ---------------------------------------------------------------------------


class EvidenceRepository:
    """Manages received source events with idempotency."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        scope: Scope,
        source_event_id: str,
        source_role: str,
        text: str,
        occurred_at: datetime,
        session_id: str | None = None,
        retention_profile: str | None = None,
        deletion_generation: int = 0,
    ) -> Evidence:
        """Create a new evidence record."""
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get_or_create(scope)
        ev = Evidence(
            scope_id=sc.id,
            source_event_id=source_event_id,
            source_role=source_role,
            text=text,
            text_hash=_hash_text(text),
            occurred_at=occurred_at,
            session_id=session_id,
            retention_profile=retention_profile,
            deletion_generation=deletion_generation,
        )
        self._session.add(ev)
        self._session.flush()
        return ev

    def get_by_event_id(self, scope: Scope, source_event_id: str) -> Evidence | None:
        """Get evidence by scope + source_event_id (for idempotency check)."""
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return None
        stmt = select(Evidence).where(
            Evidence.scope_id == sc.id,
            Evidence.source_event_id == source_event_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def check_idempotency(
        self, scope: Scope, source_event_id: str, text: str
    ) -> tuple[str, Evidence | None]:
        """Check idempotency: (status, existing_evidence).

        Returns:
            "new" — no existing record
            "idempotent" — existing record with same hash
            "conflict" — existing record with different hash
        """
        existing = self.get_by_event_id(scope, source_event_id)
        if existing is None:
            return "new", None
        if existing.text_hash == _hash_text(text):
            return "idempotent", existing
        return "conflict", existing

    def get_by_id(self, evidence_id: str) -> Evidence | None:
        return self._session.get(Evidence, evidence_id)

    def lock_by_id(self, evidence_id: str) -> Evidence | None:
        """SELECT FOR UPDATE — blocks concurrent evidence deletion."""
        stmt = select(Evidence).where(Evidence.id == evidence_id).with_for_update()
        return self._session.execute(stmt).scalar_one_or_none()

    def list_by_scope(self, scope: Scope, limit: int = 100) -> Sequence[Evidence]:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return []
        stmt = (
            select(Evidence)
            .where(Evidence.scope_id == sc.id)
            .order_by(Evidence.created_at.desc())
            .limit(limit)
        )
        return self._session.execute(stmt).scalars().all()


# ---------------------------------------------------------------------------
# Job Repository
# ---------------------------------------------------------------------------


class JobRepository:
    """Manages processing jobs with lease/fencing support."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        scope: Scope,
        evidence_id: str,
        purpose: str | None = None,
        accepted_auth_version: int = 0,
        accepted_deletion_generation: int = 0,
    ) -> Job:
        """Create a new pending job with frozen authorization snapshot."""
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get_or_create(scope)
        job = Job(
            scope_id=sc.id,
            evidence_id=evidence_id,
            state="pending",
            purpose=purpose,
            accepted_auth_version=accepted_auth_version,
            accepted_deletion_generation=accepted_deletion_generation,
        )
        self._session.add(job)
        self._session.flush()
        return job

    def get_by_id(self, job_id: str) -> Job | None:
        return self._session.get(Job, job_id)

    def get_state(self, job_id: str) -> str | None:
        job = self.get_by_id(job_id)
        return job.state if job else None

    def lock_running_job(self, job_id: str, lease_token: int, lease_owner: str) -> Job | None:
        """SELECT FOR UPDATE on a running job with matching token, owner, and unexpired lease.

        This prevents an expired-but-not-yet-reclaimed worker from publishing.
        """
        now = _utcnow()
        stmt = (
            select(Job)
            .where(
                Job.id == job_id,
                Job.state == "running",
                Job.lease_token == lease_token,
                Job.lease_owner == lease_owner,
                Job.lease_expires_at > now,
            )
            .with_for_update()
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def try_acquire_lease(
        self,
        job_id: str,
        owner: str,
        ttl_seconds: int = 300,
        scope_id: str | None = None,
    ) -> tuple[bool, int]:
        """Attempt to acquire a lease using true CAS (compare-and-swap).

        Uses conditional UPDATE with RETURNING for database-level atomicity.
        Allows reclaiming "running" jobs whose lease has expired.

        If scope_id is provided, the job must also belong to that scope,
        preventing cross-scope lease hijacking.

        Returns (acquired, lease_token).
        """
        from sqlalchemy import update

        now = _utcnow()
        new_token_subquery = Job.lease_token + 1

        pending_cond = Job.state == "pending"
        expired_running_cond = (
            (Job.state == "running")
            & (Job.lease_expires_at.isnot(None))
            & (Job.lease_expires_at < now)
        )

        where_conditions = [
            Job.id == job_id,
            pending_cond | expired_running_cond,
        ]
        if scope_id is not None:
            where_conditions.append(Job.scope_id == scope_id)

        stmt = (
            update(Job)
            .where(*where_conditions)
            .values(
                lease_token=new_token_subquery,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=ttl_seconds),
                state="running",
            )
            .returning(Job.lease_token)
        )

        result = self._session.execute(stmt)
        row = result.first()
        self._session.flush()

        if row is None:
            job = self.get_by_id(job_id)
            return False, job.lease_token if job else 0

        return True, row[0]

    def complete(self, job_id: str, lease_token: int, reason: str | None = None) -> bool:
        """Mark job as ready using database-level fencing.

        Uses conditional UPDATE: only succeeds if the job is still 'running'
        and the lease_token matches. This prevents a stale worker (whose lease
        was reclaimed) from completing a job that a new worker now owns.

        Returns True if the update affected exactly one row.
        """
        from sqlalchemy import update

        stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.state == "running",
                Job.lease_token == lease_token,
            )
            .values(state="ready", reason=reason)
        )
        result = self._session.execute(stmt)
        self._session.flush()
        # Expire only the affected job, not the entire identity map
        cached_job = self._session.identity_map.get(self._session.identity_key(Job, (job_id,)))
        if cached_job is not None:
            self._session.expire(cached_job)
        return bool(result.rowcount == 1)  # type: ignore[attr-defined]

    def fail(self, job_id: str, lease_token: int, reason: str) -> bool:
        """Mark job as failed/retryable using database-level fencing.

        Uses conditional UPDATE with lease token check.
        Increments retry_count atomically.
        """
        from sqlalchemy import update

        # First, read current retry_count to decide final state
        job = self.get_by_id(job_id)
        if job is None:
            return False

        new_state = "failed" if job.retry_count + 1 >= job.max_retries else "pending"

        stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.state == "running",
                Job.lease_token == lease_token,
            )
            .values(
                state=new_state,
                reason=reason,
                retry_count=Job.retry_count + 1,
            )
        )
        result = self._session.execute(stmt)
        self._session.flush()
        cached_job = self._session.identity_map.get(self._session.identity_key(Job, (job_id,)))
        if cached_job is not None:
            self._session.expire(cached_job)
        return bool(result.rowcount == 1)  # type: ignore[attr-defined]

    def cancel(self, job_id: str) -> bool:
        """Cancel a job (e.g., due to revocation).

        Cancel is unconditional — any state can be cancelled.
        """
        job = self.get_by_id(job_id)
        if job is None:
            return False
        job.state = "cancelled"
        self._session.flush()
        return True

    def find_pending(self, scope: Scope, limit: int = 10) -> Sequence[Job]:
        """Find pending jobs for a scope."""
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return []
        stmt = (
            select(Job)
            .where(Job.scope_id == sc.id, Job.state == "pending")
            .order_by(Job.created_at)
            .limit(limit)
        )
        return self._session.execute(stmt).scalars().all()


# ---------------------------------------------------------------------------
# Memory Atom Repository
# ---------------------------------------------------------------------------


class MemoryAtomRepository:
    """Manages extracted memory atoms."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        scope: Scope,
        type: str,
        content: str,
        source_kind: str,
        source_evidence_id: str | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        selection_reason: str | None = None,
    ) -> MemoryAtomModel:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get_or_create(scope)
        atom = MemoryAtomModel(
            scope_id=sc.id,
            type=type,
            content=content,
            source_kind=source_kind,
            source_evidence_id=source_evidence_id,
            valid_from=valid_from,
            valid_until=valid_until,
            selection_reason=selection_reason,
            is_current=True,
            deletion_generation=sc.deletion_generation,
        )
        self._session.add(atom)
        self._session.flush()
        return atom

    def get_by_id(self, memory_id: str) -> MemoryAtomModel | None:
        return self._session.get(MemoryAtomModel, memory_id)

    def get_current(self, scope: Scope, memory_type: str | None = None) -> Sequence[MemoryAtomModel]:
        """Get current (non-superseded) atoms for scope."""
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return []
        stmt = select(MemoryAtomModel).where(
            MemoryAtomModel.scope_id == sc.id,
            MemoryAtomModel.is_current == True,
            MemoryAtomModel.superseded_by == None,
        )
        if memory_type:
            stmt = stmt.where(MemoryAtomModel.type == memory_type)
        stmt = stmt.order_by(MemoryAtomModel.created_at.desc())
        return self._session.execute(stmt).scalars().all()

    def get_history(self, scope: Scope, memory_type: str | None = None) -> Sequence[MemoryAtomModel]:
        """Get all atoms including superseded ones."""
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return []
        stmt = select(MemoryAtomModel).where(MemoryAtomModel.scope_id == sc.id)
        if memory_type:
            stmt = stmt.where(MemoryAtomModel.type == memory_type)
        stmt = stmt.order_by(MemoryAtomModel.created_at.desc())
        return self._session.execute(stmt).scalars().all()

    def supersede(self, old_atom_id: str, new_atom_id: str) -> bool:
        """Mark old atom as superseded by new atom."""
        old = self.get_by_id(old_atom_id)
        if old is None:
            return False
        old.is_current = False
        old.superseded_by = new_atom_id
        self._session.flush()
        return True

    def mark_corrected(self, atom_id: str, revision: int) -> tuple[bool, int]:
        """Mark atom as corrected. Returns (success, actual_revision)."""
        atom = self.get_by_id(atom_id)
        if atom is None:
            return False, 0
        if atom.revision != revision:
            return False, atom.revision
        atom.is_current = False
        self._session.flush()
        return True, atom.revision

    def soft_delete_by_scope(self, scope: Scope, generation: int) -> int:
        """Mark all atoms in scope as deleted with given generation. Returns count."""
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return 0
        eligible = (
            self._session.query(MemoryAtomModel)
            .filter(
                MemoryAtomModel.scope_id == sc.id,
                MemoryAtomModel.deletion_generation < generation,
            )
            .all()
        )
        count = len(eligible)
        for atom in eligible:
            atom.is_current = False
            atom.deletion_generation = generation
        self._session.flush()
        return count

    def soft_delete_by_ids(self, scope: Scope, memory_ids: list[str], generation: int) -> int:
        """Delete specific atoms by ID with scope verification. Returns count deleted."""
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return 0
        count = 0
        for mid in memory_ids:
            atom = self.get_by_id(mid)
            if atom and atom.scope_id == sc.id:
                atom.is_current = False
                atom.deletion_generation = generation
                count += 1
        self._session.flush()
        return count

    def soft_delete_by_evidence_ids(self, scope: Scope, evidence_ids: list[str], generation: int) -> tuple[int, int]:
        """Delete evidence and their derived atoms. Returns (atoms_deleted, evidence_deleted)."""
        ev_repo = EvidenceRepository(self._session)
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return 0, 0

        evidence_deleted = 0
        atoms_deleted = 0
        for eid in evidence_ids:
            ev = ev_repo.get_by_id(eid)
            if ev and ev.scope_id == sc.id:
                ev.deletion_generation = generation
                evidence_deleted += 1
                # Find atoms derived from this evidence
                derived = (
                    self._session.query(MemoryAtomModel)
                    .filter(
                        MemoryAtomModel.scope_id == sc.id,
                        MemoryAtomModel.source_evidence_id == eid,
                        MemoryAtomModel.is_current == True,
                    )
                    .all()
                )
                for atom in derived:
                    atom.is_current = False
                    atom.deletion_generation = generation
                    atoms_deleted += 1
        self._session.flush()
        return atoms_deleted, evidence_deleted


# ---------------------------------------------------------------------------
# View Head Repository
# ---------------------------------------------------------------------------


class ViewHeadRepository:
    """Manages current view with optimistic locking (compare-and-swap)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, scope: Scope) -> ViewHead | None:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return None
        return self._session.get(ViewHead, sc.id)

    def get_revision(self, scope: Scope) -> int:
        vh = self.get(scope)
        return vh.revision if vh else 0

    def compare_and_swap(
        self,
        scope: Scope,
        expected_revision: int,
        content_hash: str,
        caused_by: str | None = None,
    ) -> tuple[bool, int]:
        """Attempt DB-level CAS update. Returns (success, current_revision).

        Uses conditional UPDATE WHERE revision = expected to prevent
        two concurrent publishes from both succeeding.
        """
        from sqlalchemy import update as sa_update

        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get_or_create(scope)

        if expected_revision == 0:
            # Create initial view head
            vh = ViewHead(scope_id=sc.id, revision=1, content_hash=content_hash)
            self._session.add(vh)
            vr = ViewRevision(
                scope_id=sc.id, revision=1,
                content={"content_hash": content_hash}, caused_by=caused_by,
            )
            self._session.add(vr)
            self._session.flush()
            return True, 1

        # Atomic CAS: UPDATE only if revision matches
        stmt = (
            sa_update(ViewHead)
            .where(
                ViewHead.scope_id == sc.id,
                ViewHead.revision == expected_revision,
            )
            .values(revision=expected_revision + 1, content_hash=content_hash)
        )
        result = self._session.execute(stmt)
        if bool(result.rowcount == 0):  # type: ignore[attr-defined]
            # Revision changed — CAS failed
            current = self.get_revision(scope)
            return False, current

        # Record revision history
        vr = ViewRevision(
            scope_id=sc.id,
            revision=expected_revision + 1,
            content={"content_hash": content_hash},
            caused_by=caused_by,
        )
        self._session.add(vr)
        self._session.flush()
        return True, expected_revision + 1


# ---------------------------------------------------------------------------
# Receipt Repository
# ---------------------------------------------------------------------------


class ReceiptRepository:
    """Manages operation receipts for audit and explain."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        scope: Scope,
        operation_type: str,
        auth_version: int,
        target_ids: list[str] | None = None,
        detail: dict | None = None,
    ) -> Receipt:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get_or_create(scope)
        receipt = Receipt(
            scope_id=sc.id,
            operation_type=operation_type,
            target_ids=target_ids,
            auth_version=auth_version,
            detail=detail,
        )
        self._session.add(receipt)
        self._session.flush()
        return receipt

    def get_by_id(self, receipt_id: str) -> Receipt | None:
        return self._session.get(Receipt, receipt_id)

    def list_by_scope(self, scope: Scope, limit: int = 50) -> Sequence[Receipt]:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return []
        stmt = (
            select(Receipt)
            .where(Receipt.scope_id == sc.id)
            .order_by(Receipt.created_at.desc())
            .limit(limit)
        )
        return self._session.execute(stmt).scalars().all()


# ---------------------------------------------------------------------------
# Deletion Job Repository
# ---------------------------------------------------------------------------


class DeletionJobRepository:
    """Manages deletion jobs with barrier semantics."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        scope: Scope,
        request_id: str,
        selector: dict,
    ) -> DeletionJob:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get_or_create(scope)
        dj = DeletionJob(
            scope_id=sc.id,
            request_id=request_id,
            selector=selector,
            status="blocked",
        )
        self._session.add(dj)
        self._session.flush()
        return dj

    def get_by_id(self, deletion_job_id: str) -> DeletionJob | None:
        return self._session.get(DeletionJob, deletion_job_id)

    def get_by_request_id(self, scope: Scope, request_id: str) -> DeletionJob | None:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return None
        stmt = select(DeletionJob).where(
            DeletionJob.scope_id == sc.id,
            DeletionJob.request_id == request_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def update_status(self, deletion_job_id: str, status: str, reason: str | None = None) -> bool:
        dj = self.get_by_id(deletion_job_id)
        if dj is None:
            return False
        dj.status = status
        if reason:
            dj.reason = reason
        if status == "verified":
            dj.verified_at = _utcnow()
        self._session.flush()
        return True


# ---------------------------------------------------------------------------
# Lifecycle Event Repository
# ---------------------------------------------------------------------------


class LifecycleEventRepository:
    """Records significant lifecycle events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        scope: Scope,
        event_type: str,
        auth_version: int | None = None,
        detail: dict | None = None,
    ) -> LifecycleEvent:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get_or_create(scope)
        ev = LifecycleEvent(
            scope_id=sc.id,
            event_type=event_type,
            auth_version=auth_version,
            detail=detail,
        )
        self._session.add(ev)
        self._session.flush()
        return ev

    def list_by_scope(self, scope: Scope, limit: int = 50) -> Sequence[LifecycleEvent]:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get(scope)
        if sc is None:
            return []
        stmt = (
            select(LifecycleEvent)
            .where(LifecycleEvent.scope_id == sc.id)
            .order_by(LifecycleEvent.created_at.desc())
            .limit(limit)
        )
        return self._session.execute(stmt).scalars().all()


# ---------------------------------------------------------------------------
# Generation Run Repository
# ---------------------------------------------------------------------------


class GenerationRunRepository:
    """Tracks model dispatch attempts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        job_id: str,
        scope: Scope,
        model_name: str | None = None,
        prompt_version: str | None = None,
        input_fingerprint: str | None = None,
    ) -> GenerationRun:
        sc_repo = ScopeControlRepository(self._session)
        sc = sc_repo.get_or_create(scope)
        run = GenerationRun(
            job_id=job_id,
            scope_id=sc.id,
            status="dispatched",
            model_name=model_name,
            prompt_version=prompt_version,
            input_fingerprint=input_fingerprint,
        )
        self._session.add(run)
        self._session.flush()
        return run

    def complete(self, run_id: str, result: dict, cost: float | None = None, tokens: int | None = None) -> bool:
        run = self._session.get(GenerationRun, run_id)
        if run is None:
            return False
        run.status = "completed"
        run.result = result
        run.cost_estimate = cost
        run.tokens_used = tokens
        run.completed_at = _utcnow()
        self._session.flush()
        return True

    def fail(self, run_id: str, error: str) -> bool:
        run = self._session.get(GenerationRun, run_id)
        if run is None:
            return False
        run.status = "failed"
        run.error = error
        run.completed_at = _utcnow()
        self._session.flush()
        return True

    def cancel(self, run_id: str) -> bool:
        run = self._session.get(GenerationRun, run_id)
        if run is None:
            return False
        run.status = "cancelled"
        run.completed_at = _utcnow()
        self._session.flush()
        return True