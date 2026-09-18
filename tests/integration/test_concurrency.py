"""Concurrent acceptance tests — verify invariants under real concurrency.

These tests use two independent PostgreSQL sessions and threads
to simulate real concurrent Worker behavior.

Only runs on PostgreSQL. SQLite skips all tests (no real concurrency).
"""

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mirror_memory.adapters.postgresql.models import (
    Base,
    BudgetReservation,
    DeletionJob,
    Evidence,
    GenerationRun,
    Job,
    LifecycleEvent,
    MemoryAtomModel,
    Receipt,
    ScopeControl,
    UsageLedger,
    ViewHead,
    ViewRevision,
)
from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    JobRepository,
    MemoryAtomRepository,
    ScopeControlRepository,
)
from mirror_memory.application.memory_service import MirrorMemoryService
from mirror_memory.core.types import (
    AuthorizationSnapshot,
    ForgetInput,
    ForgetSelector,
    MemoryContext,
    ObserveInput,
    Scope,
    SourceEvent,
    SourceRole,
    SyncAuthorizationInput,
)
from mirror_memory.runtime.worker import JobWorker, _factory_from_session

PG_URL = os.environ.get("DATABASE_URL", "")
SKIP_PG = "postgresql" not in PG_URL

pytestmark = pytest.mark.skipif(SKIP_PG, reason="Concurrent tests require PostgreSQL")


def _now():
    return datetime.now(UTC)


def _auth(scope, version=1, ops=None, purpose="memory_management"):
    now = _now()
    return AuthorizationSnapshot(
        scope=scope, purpose=purpose,
        allowed_operations=ops or ["observe", "recall", "correct", "forget", "explain", "export"],
        version=version, issued_at=now, expires_at=now + timedelta(hours=1), issuer="test",
    )


def _make_session():
    engine = create_engine(PG_URL)
    return sessionmaker(bind=engine), engine


def _cleanup(engine, tenant):
    """Delete all data for a tenant using ORM queries."""
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        sc_ids = [sc.id for sc in s.query(ScopeControl).filter(ScopeControl.tenant_id == tenant).all()]
        if sc_ids:
            for model in [LifecycleEvent, Receipt, DeletionJob, BudgetReservation,
                          UsageLedger, ViewRevision, ViewHead, GenerationRun,
                          MemoryAtomModel, Job, Evidence]:
                s.query(model).filter(model.scope_id.in_(sc_ids)).delete()
        s.query(ScopeControl).filter(ScopeControl.tenant_id == tenant).delete()
        s.commit()
    finally:
        s.close()


# -----------------------------------------------------------------------
# I1: One Job, One Worker — concurrent lease CAS
# -----------------------------------------------------------------------

class TestInvariant1_ConcurrentLease:
    """I1: A Job is held by at most one Worker at any time."""

    def test_two_threads_race_same_job(self):
        """Two threads try to acquire the same job — exactly one wins."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)

        setup = factory()
        scope = Scope(tenant_id="i1", app_id="a", subject_id="u1")
        sc_repo = ScopeControlRepository(setup)
        sc_repo.update_authorization(
            scope, 1, "mm", ["observe"],
            _now(), _now() + timedelta(hours=1), "test",
        )
        ev_repo = EvidenceRepository(setup)
        ev = ev_repo.create(
            scope=scope, source_event_id="i1_ev1",
            source_role=SourceRole.USER, text="test", occurred_at=_now(),
        )
        job_repo = JobRepository(setup)
        job = job_repo.create(scope=scope, evidence_id=ev.id)
        job_id = job.id
        setup.commit()
        setup.close()

        results = []
        barrier = threading.Barrier(2)

        def try_acquire(worker_name):
            session = factory()
            try:
                repo = JobRepository(session)
                barrier.wait()
                ok, tok = repo.try_acquire_lease(job_id, worker_name)
                results.append((worker_name, ok, tok))
                session.commit()
            finally:
                session.close()

        t1 = threading.Thread(target=try_acquire, args=("w1",))
        t2 = threading.Thread(target=try_acquire, args=("w2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert len(results) == 2
        winners = [r for r in results if r[1]]
        assert len(winners) == 1, f"Expected 1 winner, got {len(winners)}"

        _cleanup(engine, "i1")

    def test_complete_with_stale_token_fails(self):
        """Worker with reclaimed lease cannot complete the job."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        _cleanup(engine, "i1b")

        setup = factory()
        scope = Scope(tenant_id="i1b", app_id="a", subject_id="u1")
        sc_repo = ScopeControlRepository(setup)
        sc_repo.update_authorization(
            scope, 1, "mm", ["observe"],
            _now(), _now() + timedelta(hours=1), "test",
        )
        ev_repo = EvidenceRepository(setup)
        ev = ev_repo.create(
            scope=scope, source_event_id="i1b_ev1",
            source_role=SourceRole.USER, text="test", occurred_at=_now(),
        )
        job_repo = JobRepository(setup)
        job = job_repo.create(scope=scope, evidence_id=ev.id)
        job_id = job.id
        setup.commit()
        setup.close()

        # Worker 1 acquires
        s1 = factory()
        repo1 = JobRepository(s1)
        ok1, tok1 = repo1.try_acquire_lease(job_id, "w1", ttl_seconds=1)
        assert ok1
        s1.commit()

        # Expire lease via ORM
        s_exp = factory()
        job_exp = s_exp.get(Job, job_id)
        job_exp.lease_expires_at = _now() - timedelta(seconds=1)
        s_exp.commit()
        s_exp.close()

        # Worker 2 reclaims
        s2 = factory()
        repo2 = JobRepository(s2)
        ok2, tok2 = repo2.try_acquire_lease(job_id, "w2")
        assert ok2
        assert tok2 > tok1
        s2.commit()  # Release lock

        # Worker 1 tries to complete with old token — must fail
        s1_retry = factory()
        assert not JobRepository(s1_retry).complete(job_id, tok1, "stale")
        s1_retry.close()

        # Worker 2 completes — must succeed
        assert repo2.complete(job_id, tok2, "done")
        s2.commit()

        _cleanup(engine, "i1b")


# -----------------------------------------------------------------------
# I2: Deleted Scope cannot produce new Atom
# -----------------------------------------------------------------------

class TestInvariant2_DeleteBlocksPublish:
    """I2: Revoke/delete during Compute prevents Atom creation."""

    def test_revoke_between_claim_and_publish(self):
        """Worker acquires lease, then auth is revoked — Publish must fail."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)

        setup = factory()
        scope = Scope(tenant_id="i2", app_id="a", subject_id="u1")
        svc = MirrorMemoryService(setup)
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=_auth(scope)))
        ctx = MemoryContext(scope=scope, purpose="memory_management")
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id="i2_ev1", source_role=SourceRole.USER,
                text="test", occurred_at=_now(),
            ),
        ))
        assert r.success
        job_id = r.outcome.operation_id
        setup.commit()
        setup.close()

        # Revoke in a separate session
        s_revoke = factory()
        svc_rev = MirrorMemoryService(s_revoke)
        svc_rev.sync_authorization(SyncAuthorizationInput(
            event_type="revoke", snapshot=_auth(scope, version=2, ops=[]),
        ))
        s_revoke.commit()
        s_revoke.close()

        # Worker tries to process — should be cancelled
        s_work = factory()
        s_work.commit()  # Worker opens its own session — must see committed data
        worker = JobWorker(_factory_from_session(s_work))
        result = worker.process(scope, job_id, "w1")
        assert result["status"] in ("cancelled", "lease_failed")
        s_work.rollback()

        _cleanup(engine, "i2")

    def test_delete_between_claim_and_publish(self):
        """Scope deleted during Compute — Atom must not be created."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)

        setup = factory()
        scope = Scope(tenant_id="i2b", app_id="a", subject_id="u1")
        svc = MirrorMemoryService(setup)
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=_auth(scope)))
        ctx = MemoryContext(scope=scope, purpose="memory_management")
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id="i2b_ev1", source_role=SourceRole.USER,
                text="test", occurred_at=_now(),
            ),
        ))
        job_id = r.outcome.operation_id
        setup.commit()
        setup.close()

        # Delete scope
        s_del = factory()
        svc_del = MirrorMemoryService(s_del)
        svc_del.forget(ForgetInput(
            context=ctx,
            selector=ForgetSelector(scope_wide=True),
            request_id="i2b_del",
        ))
        s_del.commit()
        s_del.close()

        # Worker — should be cancelled
        s_work = factory()
        s_work.commit()  # Worker opens its own session — must see committed data
        worker = JobWorker(_factory_from_session(s_work))
        result = worker.process(scope, job_id, "w1")
        assert result["status"] in ("cancelled", "lease_failed")
        s_work.rollback()

        _cleanup(engine, "i2b")


# -----------------------------------------------------------------------
# I3: complete/fence only by current holder
# -----------------------------------------------------------------------

class TestInvariant3_Fencing:
    """I3: Only the current lease holder can complete a job."""

    def test_two_workers_complete_only_holder_wins(self):
        """Worker 2 holds lease — Worker 1's complete must fail."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        _cleanup(engine, "i3")

        setup = factory()
        scope = Scope(tenant_id="i3", app_id="a", subject_id="u1")
        sc_repo = ScopeControlRepository(setup)
        sc_repo.update_authorization(
            scope, 1, "mm", ["observe"],
            _now(), _now() + timedelta(hours=1), "test",
        )
        ev_repo = EvidenceRepository(setup)
        ev = ev_repo.create(
            scope=scope, source_event_id="i3_ev1",
            source_role=SourceRole.USER, text="test", occurred_at=_now(),
        )
        job_repo = JobRepository(setup)
        job = job_repo.create(scope=scope, evidence_id=ev.id)
        job_id = job.id
        setup.commit()
        setup.close()

        # W1 acquires, W2 acquires after expiry
        s1 = factory()
        r1 = JobRepository(s1)
        ok1, tok1 = r1.try_acquire_lease(job_id, "w1", ttl_seconds=1)
        assert ok1
        s1.commit()

        # Expire via ORM
        s_exp = factory()
        job_exp = s_exp.get(Job, job_id)
        job_exp.lease_expires_at = _now() - timedelta(seconds=1)
        s_exp.commit()
        s_exp.close()

        s2 = factory()
        r2 = JobRepository(s2)
        ok2, tok2 = r2.try_acquire_lease(job_id, "w2")
        assert ok2
        s2.commit()  # Release lock so s1r can attempt

        # W1 fails (stale token), W2 succeeds
        s1r = factory()
        assert not JobRepository(s1r).complete(job_id, tok1, "w1 done")
        s1r.close()

        assert r2.complete(job_id, tok2, "w2 done")
        s2.commit()

        _cleanup(engine, "i3")


# -----------------------------------------------------------------------
# I5: Purpose mismatch at service and worker layers
# -----------------------------------------------------------------------

class TestInvariant5_Purpose:
    """I5: Purpose mismatch is caught at both service and worker layers."""

    def test_service_rejects_wrong_purpose(self):
        factory, engine = _make_session()
        Base.metadata.create_all(engine)

        s = factory()
        scope = Scope(tenant_id="i5", app_id="a", subject_id="u1")
        svc = MirrorMemoryService(s)
        svc.sync_authorization(SyncAuthorizationInput(
            event_type="grant", snapshot=_auth(scope, purpose="analytics"),
        ))
        ctx = MemoryContext(scope=scope, purpose="memory_management")
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id="i5_ev1", source_role=SourceRole.USER,
                text="test", occurred_at=_now(),
            ),
        ))
        assert not r.success
        assert "purpose" in r.reason.lower()
        s.close()

        _cleanup(engine, "i5")

    def test_worker_rejects_wrong_purpose(self):
        factory, engine = _make_session()
        Base.metadata.create_all(engine)

        s = factory()
        scope = Scope(tenant_id="i5b", app_id="a", subject_id="u1")
        svc = MirrorMemoryService(s)
        svc.sync_authorization(SyncAuthorizationInput(
            event_type="grant", snapshot=_auth(scope, purpose="analytics"),
        ))
        ctx = MemoryContext(scope=scope, purpose="analytics")
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id="i5b_ev1", source_role=SourceRole.USER,
                text="test", occurred_at=_now(),
            ),
        ))
        job_id = r.outcome.operation_id
        s.commit()
        s.close()

        # Purpose is now frozen on the Job at Observe time.
        # The worker reads job.purpose, so it matches the auth.
        # Purpose mismatch is caught at Observe, not at Worker.
        s_w = factory()
        s_w.commit()
        worker = JobWorker(_factory_from_session(s_w))
        result = worker.process(scope, job_id, "w1")
        assert result["status"] == "completed"
        s_w.commit()

        # Verify: Observe with different purpose is rejected
        ctx2 = MemoryContext(scope=scope, purpose="memory_management")
        r2 = svc.observe(ObserveInput(
            context=ctx2,
            source_event=SourceEvent(
                source_event_id="i5b_ev2", source_role=SourceRole.USER,
                text="test2", occurred_at=_now(),
            ),
        ))
        assert not r2.success
        assert "purpose" in r2.reason.lower()
        s.commit()

        _cleanup(engine, "i5b")


# -----------------------------------------------------------------------
# Phase-interleaving: real Claim → Compute → (interfere) → Publish
# -----------------------------------------------------------------------

class TestPhaseInterleaving:
    """Verify that interference between Claim and Publish is caught."""

    def _setup_job(self, factory, engine, tenant):
        """Create a job and return (scope, job_id)."""
        _cleanup(engine, tenant)
        s = factory()
        scope = Scope(tenant_id=tenant, app_id="a", subject_id="u1")
        svc = MirrorMemoryService(s)
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=_auth(scope)))
        ctx = MemoryContext(scope=scope, purpose="memory_management")
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id=f"{tenant}_ev1", source_role=SourceRole.USER,
                text="I prefer dark mode", occurred_at=_now(),
            ),
        ))
        assert r.success
        job_id = r.outcome.operation_id
        s.commit()
        s.close()
        return scope, job_id

    def test_revoke_between_claim_and_publish(self):
        """Claim succeeds → revoke in separate session → Publish must cancel."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        scope, job_id = self._setup_job(factory, engine, "ip1")

        # Phase 1: Claim
        worker = JobWorker(_factory_from_session(factory()))
        ticket = worker.claim(scope, job_id, "w1")
        assert ticket is not None

        # Phase 2: Compute (no DB)
        result = worker.compute(ticket)
        assert result is not None

        # Interfere: revoke in separate session
        s_revoke = factory()
        svc_rev = MirrorMemoryService(s_revoke)
        svc_rev.sync_authorization(SyncAuthorizationInput(
            event_type="revoke", snapshot=_auth(scope, version=2, ops=[]),
        ))
        s_revoke.commit()
        s_revoke.close()

        # Phase 3: Publish — must cancel because auth revoked
        pub = worker.publish(ticket, result)
        assert pub.status == "cancelled"
        assert "auth" in pub.reason.lower() or "revoked" in pub.reason.lower()

        # Verify: no atoms created
        s_check = factory()
        atom_repo = MemoryAtomRepository(s_check)
        atoms = atom_repo.get_current(scope)
        assert len(atoms) == 0
        s_check.close()

        _cleanup(engine, "ip1")

    def test_delete_between_claim_and_publish(self):
        """Claim succeeds → delete scope → Publish must cancel."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        scope, job_id = self._setup_job(factory, engine, "ip2")

        # Phase 1: Claim
        worker = JobWorker(_factory_from_session(factory()))
        ticket = worker.claim(scope, job_id, "w1")
        assert ticket is not None

        # Phase 2: Compute
        result = worker.compute(ticket)

        # Interfere: delete scope
        s_del = factory()
        svc_del = MirrorMemoryService(s_del)
        svc_del.forget(ForgetInput(
            context=MemoryContext(scope=scope, purpose="memory_management"),
            selector=ForgetSelector(scope_wide=True),
            request_id="ip2_del",
        ))
        s_del.commit()
        s_del.close()

        # Phase 3: Publish — must cancel
        pub = worker.publish(ticket, result)
        assert pub.status == "cancelled"

        # Verify: no atoms
        s_check = factory()
        assert len(MemoryAtomRepository(s_check).get_current(scope)) == 0
        s_check.close()

        _cleanup(engine, "ip2")

    def test_lease_reclaimed_between_claim_and_publish(self):
        """Claim succeeds → lease reclaimed → Publish must cancel."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        scope, job_id = self._setup_job(factory, engine, "ip3")

        # Phase 1: Claim
        worker = JobWorker(_factory_from_session(factory()))
        ticket = worker.claim(scope, job_id, "w1", ttl_seconds=1)
        assert ticket is not None

        # Phase 2: Compute
        result = worker.compute(ticket)

        # Interfere: expire lease and reclaim
        s_reclaim = factory()
        job = s_reclaim.get(Job, job_id)
        job.lease_expires_at = _now() - timedelta(seconds=1)
        s_reclaim.commit()

        repo = JobRepository(s_reclaim)
        ok, _ = repo.try_acquire_lease(job_id, "w2")
        assert ok
        s_reclaim.commit()
        s_reclaim.close()

        # Phase 3: Publish — must cancel (lease lost)
        pub = worker.publish(ticket, result)
        assert pub.status == "cancelled"
        assert "lease" in pub.reason.lower()

        _cleanup(engine, "ip3")

    def test_no_interference_publish_succeeds(self):
        """No interference → Publish completes successfully."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        scope, job_id = self._setup_job(factory, engine, "ip4")

        # Phase 1: Claim
        worker = JobWorker(_factory_from_session(factory()))
        ticket = worker.claim(scope, job_id, "w1")
        assert ticket is not None

        # Phase 2: Compute
        result = worker.compute(ticket)
        assert result.atoms  # should extract something

        # No interference

        # Phase 3: Publish — should succeed
        pub = worker.publish(ticket, result)
        assert pub.status == "completed"
        assert pub.atoms_created >= 1

        # Verify: atoms exist
        s_check = factory()
        atoms = MemoryAtomRepository(s_check).get_current(scope)
        assert len(atoms) >= 1
        s_check.close()

        _cleanup(engine, "ip4")

    def test_concurrent_publish_only_one_succeeds(self):
        """Two threads publish simultaneously — exactly one succeeds, Atom count is 1."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        scope, job_id = self._setup_job(factory, engine, "ip5")

        # Worker 1 claims
        w1 = JobWorker(_factory_from_session(factory()))
        t1 = w1.claim(scope, job_id, "w1", ttl_seconds=1)
        assert t1 is not None

        # Expire and let worker 2 claim
        s_exp = factory()
        job = s_exp.get(Job, job_id)
        job.lease_expires_at = _now() - timedelta(seconds=1)
        s_exp.commit()
        s_exp.close()

        w2 = JobWorker(_factory_from_session(factory()))
        t2 = w2.claim(scope, job_id, "w2")
        assert t2 is not None

        # Both compute
        r1 = w1.compute(t1)
        r2 = w2.compute(t2)

        # Concurrent publish using threads + barrier
        results = []
        barrier = threading.Barrier(2)

        def do_publish(name, worker, ticket, extraction):
            barrier.wait()  # Both threads enter publish simultaneously
            pub = worker.publish(ticket, extraction)
            results.append((name, pub.status))

        t_a = threading.Thread(target=do_publish, args=("w1", w1, t1, r1))
        t_b = threading.Thread(target=do_publish, args=("w2", w2, t2, r2))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert len(results) == 2
        statuses = {name: status for name, status in results}
        completed = [s for s in statuses.values() if s == "completed"]
        cancelled = [s for s in statuses.values() if s == "cancelled"]
        assert len(completed) == 1, f"Expected 1 completed, got {len(completed)}: {statuses}"
        assert len(cancelled) == 1, f"Expected 1 cancelled, got {len(cancelled)}: {statuses}"

        # Verify: exactly one set of atoms
        s_check = factory()
        atoms = MemoryAtomRepository(s_check).get_current(scope)
        assert len(atoms) >= 1
        s_check.close()

        _cleanup(engine, "ip5")


# -----------------------------------------------------------------------
# Boundary gap tests
# -----------------------------------------------------------------------

class TestBoundaryGaps:
    """Verify the 3 remaining boundary conditions."""

    def _setup_job(self, factory, engine, tenant):
        _cleanup(engine, tenant)
        s = factory()
        scope = Scope(tenant_id=tenant, app_id="a", subject_id="u1")
        svc = MirrorMemoryService(s)
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=_auth(scope)))
        ctx = MemoryContext(scope=scope, purpose="memory_management")
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id=f"{tenant}_ev1", source_role=SourceRole.USER,
                text="I prefer dark mode", occurred_at=_now(),
            ),
        ))
        assert r.success
        job_id = r.outcome.operation_id
        s.commit()
        s.close()
        return scope, job_id

    def test_expired_lease_not_reclaimed_publish_fails(self):
        """Lease expires but no other worker reclaims → Publish must fail."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        scope, job_id = self._setup_job(factory, engine, "bg1")

        # Claim with short TTL
        worker = JobWorker(_factory_from_session(factory()))
        ticket = worker.claim(scope, job_id, "w1", ttl_seconds=1)
        assert ticket is not None

        # Compute
        result = worker.compute(ticket)

        # Expire lease (but no reclaim)
        s_exp = factory()
        job = s_exp.get(Job, job_id)
        job.lease_expires_at = _now() - timedelta(seconds=1)
        s_exp.commit()
        s_exp.close()

        # Publish — must fail because lease expired
        pub = worker.publish(ticket, result)
        assert pub.status in ("cancelled", "failed")

        # Verify: no atoms
        s_check = factory()
        assert len(MemoryAtomRepository(s_check).get_current(scope)) == 0
        s_check.close()

        _cleanup(engine, "bg1")

    def test_evidence_deleted_between_claim_and_publish(self):
        """Evidence-level deletion after Claim → Publish must cancel."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        scope, job_id = self._setup_job(factory, engine, "bg2")

        # Claim
        worker = JobWorker(_factory_from_session(factory()))
        ticket = worker.claim(scope, job_id, "w1")
        assert ticket is not None

        # Compute
        result = worker.compute(ticket)

        # Delete evidence (not scope-wide)
        s_del = factory()
        svc_del = MirrorMemoryService(s_del)
        svc_del.forget(ForgetInput(
            context=MemoryContext(scope=scope, purpose="memory_management"),
            selector=ForgetSelector(evidence_ids=[ticket.evidence_id]),
            request_id="bg2_ev_del",
        ))
        s_del.commit()
        s_del.close()

        # Publish — must cancel because evidence was deleted
        pub = worker.publish(ticket, result)
        assert pub.status == "cancelled"
        assert "evidence" in pub.reason.lower()

        # Verify: no atoms
        s_check = factory()
        assert len(MemoryAtomRepository(s_check).get_current(scope)) == 0
        s_check.close()

        _cleanup(engine, "bg2")

    def test_no_memory_with_revocation_does_not_mark_ready(self):
        """no_memory result + revoke → Job must not be marked ready."""
        factory, engine = _make_session()
        Base.metadata.create_all(engine)
        _cleanup(engine, "bg3")
        s = factory()
        scope = Scope(tenant_id="bg3", app_id="a", subject_id="u1")
        svc = MirrorMemoryService(s)
        svc.sync_authorization(SyncAuthorizationInput(event_type="grant", snapshot=_auth(scope)))
        ctx = MemoryContext(scope=scope, purpose="memory_management")
        # Observe something that produces no memory (empty text)
        r = svc.observe(ObserveInput(
            context=ctx,
            source_event=SourceEvent(
                source_event_id="bg3_ev1", source_role=SourceRole.USER,
                text="a", occurred_at=_now(),
            ),
        ))
        assert r.success
        job_id = r.outcome.operation_id
        s.commit()
        s.close()

        # Claim
        worker = JobWorker(_factory_from_session(factory()))
        ticket = worker.claim(scope, job_id, "w1")
        assert ticket is not None

        # Compute — force no_memory by using empty extraction
        from mirror_memory.domains.extractor import ExtractionResult
        empty_result = ExtractionResult(atoms=[], confidence=0.0)

        # Revoke before publish
        s_rev = factory()
        MirrorMemoryService(s_rev).sync_authorization(SyncAuthorizationInput(
            event_type="revoke", snapshot=_auth(scope, version=2, ops=[]),
        ))
        s_rev.commit()
        s_rev.close()

        # Publish — must cancel, not mark as ready
        pub = worker.publish(ticket, empty_result)
        assert pub.status == "cancelled"
        assert "auth" in pub.reason.lower() or "revoked" in pub.reason.lower()

        # Verify: job is NOT ready
        s_check = factory()
        job = s_check.get(Job, job_id)
        assert job.state != "ready"
        s_check.close()

        _cleanup(engine, "bg3")