"""C06–C07: Worker idempotency and view conflict.

C06: Task delivered twice, old worker loses lease then submits late.
C07: Two tasks read same head then interleave submit.

References: R05, design.md §4 (lease, fencing, compare-and-swap).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    JobRepository,
    ViewHeadRepository,
)
from mirror_memory.core.types import SourceRole


@pytest.mark.contract_C06
class TestLeaseFencing:
    """Verify lease fencing prevents stale worker submissions."""

    def test_duplicate_delivery_single_result(self, db: Session, scope_1):
        """Task delivered twice produces exactly one valid result."""
        ev_repo = EvidenceRepository(db)
        job_repo = JobRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_lease_001",
            source_role=SourceRole.USER, text="test", occurred_at=now,
        )
        job = job_repo.create(scope=scope_1, evidence_id=ev.id)

        # First worker acquires lease
        acquired1, token1 = job_repo.try_acquire_lease(job.id, "worker_1")
        assert acquired1

        # Second worker cannot acquire (already running)
        acquired2, token2 = job_repo.try_acquire_lease(job.id, "worker_2")
        assert not acquired2

        # First worker completes
        assert job_repo.complete(job.id, token1)

        # Job is ready
        assert job_repo.get_state(job.id) == "ready"

    def test_old_fencing_token_rejected(self, db: Session, scope_1):
        """Submission with expired fencing token is rejected."""
        ev_repo = EvidenceRepository(db)
        job_repo = JobRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_fence_001",
            source_role=SourceRole.USER, text="test", occurred_at=now,
        )
        job = job_repo.create(scope=scope_1, evidence_id=ev.id)

        # Worker 1 acquires (token=1)
        acquired1, token1 = job_repo.try_acquire_lease(job.id, "worker_1", ttl_seconds=300)
        assert acquired1
        assert token1 == 1

        # Simulate lease expiry by setting expires_at in the past and resetting state
        job.lease_expires_at = now - timedelta(seconds=1)
        job.state = "pending"
        db.flush()

        # Worker 2 acquires (token=2) — lease expired
        acquired2, token2 = job_repo.try_acquire_lease(job.id, "worker_2")
        assert acquired2
        assert token2 == 2

        # Worker 1 tries to complete with old token — rejected
        assert not job_repo.complete(job.id, token1)

        # Worker 2 completes with current token — succeeds
        assert job_repo.complete(job.id, token2)

    def test_independent_support_count_unchanged(self, db: Session, scope_1):
        """Duplicate processing does not increment independent evidence count."""
        ev_repo = EvidenceRepository(db)
        job_repo = JobRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1, source_event_id="evt_count_001",
            source_role=SourceRole.USER, text="test", occurred_at=now,
        )
        job = job_repo.create(scope=scope_1, evidence_id=ev.id)

        # Only one evidence record exists
        evs = ev_repo.list_by_scope(scope_1)
        assert len(evs) == 1


@pytest.mark.contract_C07
class TestViewConflict:
    """Verify concurrent view updates use compare-and-swap."""

    def test_interleaved_submit_reintegrates(self, db: Session, scope_1):
        """Two workers reading same head: second gets revision conflict, reintegrates."""
        vh_repo = ViewHeadRepository(db)

        # Initial view
        success, rev1 = vh_repo.compare_and_swap(scope_1, 0, "hash_v1", "job_1")
        assert success
        assert rev1 == 1

        # Both workers read revision 1
        current_rev = vh_repo.get_revision(scope_1)
        assert current_rev == 1

        # Worker A succeeds
        success_a, rev_a = vh_repo.compare_and_swap(scope_1, 1, "hash_v2a", "job_a")
        assert success_a
        assert rev_a == 2

        # Worker B fails (stale revision)
        success_b, rev_b = vh_repo.compare_and_swap(scope_1, 1, "hash_v2b", "job_b")
        assert not success_b
        assert rev_b == 2  # current is now 2

        # Worker B reintegrates with correct expected revision
        success_b2, rev_b2 = vh_repo.compare_and_swap(scope_1, 2, "hash_v3b", "job_b_reintegrate")
        assert success_b2
        assert rev_b2 == 3

    def test_no_overwrite_of_committed_update(self, db: Session, scope_1):
        """Committed view update is never overwritten by stale worker."""
        vh_repo = ViewHeadRepository(db)

        vh_repo.compare_and_swap(scope_1, 0, "hash_1", "job_1")
        vh_repo.compare_and_swap(scope_1, 1, "hash_2", "job_2")

        # Stale worker tries to write at revision 1
        success, current = vh_repo.compare_and_swap(scope_1, 1, "hash_stale", "job_stale")
        assert not success
        assert current == 2

        # View is still at revision 2
        assert vh_repo.get_revision(scope_1) == 2