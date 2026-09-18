"""JobWorker — Claim → Compute → Publish three-phase architecture.

Each phase has its own transaction boundary:
- Claim (T1): CAS lease, auth check, load evidence snapshot → commit → immutable ticket
- Compute: no session, no DB — pure extraction
- Publish (T2): re-verify, write atoms, CAS view, fencing complete → commit or rollback

The worker does NOT hold a session across phases. Each phase creates
its own session from the session_factory.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    JobRepository,
    MemoryAtomRepository,
    ScopeControlRepository,
    ViewHeadRepository,
)
from mirror_memory.core.types import Scope
from mirror_memory.domains.extractor import DeterministicExtractor, ExtractionResult

# ---------------------------------------------------------------------------
# Immutable tickets — carry data across phase boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimTicket:
    """Immutable snapshot from Claim phase. Used by Compute and Publish.

    Everything Publish needs to re-verify is captured here.
    """

    job_id: str
    scope_id: str
    evidence_id: str
    lease_token: int
    lease_owner: str
    purpose: str | None
    authorization_version: int
    deletion_generation: int
    # Evidence snapshot for Compute (no DB access needed)
    evidence_text: str
    evidence_source_role: str
    evidence_scope_id: str
    evidence_deletion_generation: int


@dataclass
class PublishResult:
    """Result of the Publish phase."""

    status: str  # "completed" | "cancelled" | "failed"
    atoms_created: int = 0
    reason: str = ""


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _factory_from_session(session: Session) -> sessionmaker:
    """Create a sessionmaker from an existing session's bind."""
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


class JobWorker:
    """Three-phase worker: Claim → Compute → Publish.

    Usage:
        worker = JobWorker(session_factory, extractor, purpose="memory_management")
        ticket = worker.claim(scope, job_id, worker_id="w1")
        if ticket is None:
            # Claim failed
            ...
        result = worker.compute(ticket)
        outcome = worker.publish(ticket, result)
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        extractor: DeterministicExtractor | None = None,
    ) -> None:
        self._factory = session_factory
        self._extractor = extractor or DeterministicExtractor()

    # -------------------------------------------------------------------
    # Phase 1: Claim (transaction T1)
    # -------------------------------------------------------------------

    def claim(
        self,
        scope: Scope,
        job_id: str,
        worker_id: str = "worker_1",
        ttl_seconds: int = 300,
    ) -> ClaimTicket | None:
        """Claim a job: CAS lease + load frozen auth context from Job.

        Opens a short transaction T1, commits on success.
        Returns an immutable ClaimTicket or None if claim fails.

        The purpose and auth_version come from the Job (frozen at Observe time),
        not from the Worker constructor. This prevents callers from bypassing
        purpose checks.
        """
        session: Session = self._factory()
        try:
            scope_repo = ScopeControlRepository(session)
            job_repo = JobRepository(session)
            ev_repo = EvidenceRepository(session)

            # 1. Get scope_id for CAS guard
            sc = scope_repo.get(scope)
            if sc is None:
                return None
            scope_id = sc.id

            # 2. CAS lease acquisition with scope guard
            acquired, lease_token = job_repo.try_acquire_lease(
                job_id, worker_id, ttl_seconds=ttl_seconds, scope_id=scope_id,
            )
            if not acquired:
                return None

            # 3. Load job and verify ownership
            job = job_repo.get_by_id(job_id)
            if job is None or job.scope_id != scope_id:
                session.rollback()
                return None

            # 4. Authorization check using frozen Job context
            allowed, _reason = scope_repo.is_authorized(
                scope, "observe", purpose=job.purpose,
            )
            if not allowed:
                session.rollback()
                return None

            # 5. Load evidence and verify scope + deletion
            evidence = ev_repo.get_by_id(job.evidence_id)
            if evidence is None or evidence.scope_id != scope_id:
                session.rollback()
                return None
            if evidence.deletion_generation > sc.deletion_generation:
                session.rollback()
                return None

            # 6. Build immutable ticket BEFORE commit (snapshot from T1)
            ticket = ClaimTicket(
                job_id=job_id,
                scope_id=scope_id,
                evidence_id=job.evidence_id,
                lease_token=lease_token,
                lease_owner=worker_id,
                purpose=job.purpose,
                authorization_version=job.accepted_auth_version,
                deletion_generation=sc.deletion_generation,
                evidence_text=evidence.text,
                evidence_source_role=evidence.source_role,
                evidence_scope_id=evidence.scope_id,
                evidence_deletion_generation=evidence.deletion_generation,
            )

            # 7. Commit T1 — lease is now held
            session.commit()
            return ticket
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -------------------------------------------------------------------
    # Phase 2: Compute (no transaction)
    # -------------------------------------------------------------------

    def compute(self, ticket: ClaimTicket) -> ExtractionResult:
        """Extract memory atoms from evidence. No DB access.

        Pure computation — can fail safely without side effects.
        """
        return self._extractor.extract(
            text=ticket.evidence_text,
            source_role=ticket.evidence_source_role,
        )

    # -------------------------------------------------------------------
    # Phase 3: Publish (transaction T2)
    # -------------------------------------------------------------------

    def publish(self, ticket: ClaimTicket, extraction: ExtractionResult) -> PublishResult:
        """Publish extraction results with atomic barrier.

        ALL Compute results enter this method — including source_invalid
        and no_memory. The transaction T2 ensures that even empty results
        go through the authorization/deletion/lease barrier.

        Uses SELECT FOR UPDATE to serialize with concurrent revoke/delete.
        Re-reads Evidence to catch evidence-level deletions.
        """
        session: Session = self._factory()
        try:
            scope_repo = ScopeControlRepository(session)
            job_repo = JobRepository(session)
            ev_repo = EvidenceRepository(session)
            atom_repo = MemoryAtomRepository(session)
            vh_repo = ViewHeadRepository(session)

            # ---- Atomic barrier: lock ScopeControl row ----
            sc = scope_repo.lock_by_id(ticket.scope_id)
            if sc is None:
                session.rollback()
                return PublishResult(status="cancelled", reason="scope_gone")

            # Re-check authorization using locked row
            scope = Scope(tenant_id=sc.tenant_id, app_id=sc.app_id, subject_id=sc.subject_id)
            allowed, reason = scope_repo.is_authorized(scope, "observe", purpose=ticket.purpose)
            if not allowed:
                session.rollback()
                return PublishResult(status="cancelled", reason=f"auth_revoked: {reason}")

            # Verify auth version hasn't regressed
            if sc.auth_version < ticket.authorization_version:
                session.rollback()
                return PublishResult(status="cancelled", reason="auth_version_regressed")

            # Verify scope-level deletion generation
            if sc.deletion_generation > ticket.evidence_deletion_generation:
                session.rollback()
                return PublishResult(status="cancelled", reason="deleted_during_compute")

            # ---- Lock Evidence: serialize with evidence-level deletion ----
            evidence = ev_repo.lock_by_id(ticket.evidence_id)
            if evidence is None:
                session.rollback()
                return PublishResult(status="cancelled", reason="evidence_gone")
            if evidence.scope_id != ticket.scope_id:
                session.rollback()
                return PublishResult(status="cancelled", reason="evidence_scope_mismatch")
            if evidence.deletion_generation > ticket.evidence_deletion_generation:
                session.rollback()
                return PublishResult(status="cancelled", reason="evidence_deleted_during_compute")

            # ---- Lock Job row (token + owner + unexpired) ----
            locked_job = job_repo.lock_running_job(
                ticket.job_id, ticket.lease_token, ticket.lease_owner,
            )
            if locked_job is None:
                session.rollback()
                return PublishResult(status="cancelled", reason="lease_lost")

            # ---- Handle source_invalid (still within T2) ----
            if not extraction.source_valid:
                job_repo.fail(ticket.job_id, ticket.lease_token, f"source_invalid: {extraction.rejection_reason}")
                session.commit()
                return PublishResult(status="failed", reason=f"source_invalid: {extraction.rejection_reason}")

            # ---- Handle no_memory (still within T2) ----
            if not extraction.atoms:
                job_repo.complete(ticket.job_id, ticket.lease_token, reason="no_memory")
                session.commit()
                return PublishResult(status="completed", atoms_created=0, reason="no_memory")

            # ---- Write atoms ----
            for atom_data in extraction.atoms:
                atom_repo.create(
                    scope=scope,
                    type=atom_data["type"],
                    content=atom_data["content"],
                    source_kind=ticket.evidence_source_role,
                    source_evidence_id=ticket.evidence_id,
                )

            # ---- Atomic CAS view update ----
            content_hash = hashlib.sha256(
                ",".join(a["content"] for a in extraction.atoms).encode()
            ).hexdigest()[:64]
            current_rev = vh_repo.get_revision(scope)
            cas_ok, _ = vh_repo.compare_and_swap(
                scope, current_rev, content_hash, ticket.job_id,
            )
            if not cas_ok:
                session.rollback()
                return PublishResult(status="cancelled", reason="view_cas_failed")

            # ---- Atomic fencing complete ----
            completed = job_repo.complete(
                ticket.job_id, ticket.lease_token, reason="extracted",
            )
            if not completed:
                session.rollback()
                return PublishResult(status="cancelled", reason="lease_lost_at_commit")

            # ---- Commit T2 — all or nothing ----
            session.commit()
            return PublishResult(
                status="completed",
                atoms_created=len(extraction.atoms),
                reason="extracted",
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -------------------------------------------------------------------
    # Convenience: process = claim + compute + publish
    # -------------------------------------------------------------------

    def process(
        self,
        scope: Scope,
        job_id: str,
        worker_id: str = "worker_1",
    ) -> dict[str, Any]:
        """Full pipeline: claim → compute → publish. Returns result dict."""
        ticket = self.claim(scope, job_id, worker_id)
        if ticket is None:
            return {"status": "lease_failed", "reason": "could not acquire lease or auth denied"}

        extraction = self.compute(ticket)
        result = self.publish(ticket, extraction)

        return {
            "status": result.status,
            "atoms_created": result.atoms_created,
            "reason": result.reason,
        }

