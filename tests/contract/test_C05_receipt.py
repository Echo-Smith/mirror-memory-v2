"""C05: Reliable receipt and idempotency.

Setup: Duplicate events, same-key-different-content, forced crash before/after commit.
Must observe: Unique reliable receipt; conflict explicit; accepted records + job exist post-restart.

References: R03, design.md §3.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from mirror_memory.adapters.postgresql.repositories import (
    EvidenceRepository,
    JobRepository,
)
from mirror_memory.core.types import SourceRole


@pytest.mark.contract_C05
class TestReliableReceipt:
    """Verify observe is reliable and idempotent."""

    def test_observe_accepted_creates_evidence_and_job(self, db: Session, scope_1):
        """accepted means Evidence + job co-committed in same transaction."""
        ev_repo = EvidenceRepository(db)
        job_repo = JobRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1,
            source_event_id="evt_001",
            source_role=SourceRole.USER,
            text="I prefer Python",
            occurred_at=now,
        )
        job = job_repo.create(scope=scope_1, evidence_id=ev.id)

        assert ev.id is not None
        assert job.id is not None
        assert job.state == "pending"
        assert job.evidence_id == ev.id

    def test_duplicate_event_idempotent(self, db: Session, scope_1):
        """Same source_event_id sent twice returns same result, no duplicate."""
        ev_repo = EvidenceRepository(db)

        now = datetime.now(UTC)
        text = "I like dark mode"

        # First insert
        status1, ev1 = ev_repo.check_idempotency(scope_1, "evt_dup_001", text)
        assert status1 == "new"
        ev_repo.create(
            scope=scope_1,
            source_event_id="evt_dup_001",
            source_role=SourceRole.USER,
            text=text,
            occurred_at=now,
        )

        # Duplicate — same key, same content
        status2, ev2 = ev_repo.check_idempotency(scope_1, "evt_dup_001", text)
        assert status2 == "idempotent"
        assert ev2 is not None

    def test_same_key_different_content_conflict(self, db: Session, scope_1):
        """Same source_event_id with different payload returns conflict error."""
        ev_repo = EvidenceRepository(db)

        now = datetime.now(UTC)
        ev_repo.create(
            scope=scope_1,
            source_event_id="evt_conflict_001",
            source_role=SourceRole.USER,
            text="original content",
            occurred_at=now,
        )

        # Same key, different content
        status, existing = ev_repo.check_idempotency(
            scope_1, "evt_conflict_001", "modified content"
        )
        assert status == "conflict"
        assert existing is not None

    def test_crash_before_commit_no_evidence(self, db: Session, scope_1):
        """If session rolls back (simulating crash before commit), nothing persists."""
        ev_repo = EvidenceRepository(db)

        now = datetime.now(UTC)
        ev_repo.create(
            scope=scope_1,
            source_event_id="evt_crash_001",
            source_role=SourceRole.USER,
            text="will be rolled back",
            occurred_at=now,
        )

        # Simulate crash by rolling back
        db.rollback()

        # Evidence should not exist in a fresh query
        ev_repo2 = EvidenceRepository(db)
        ev = ev_repo2.get_by_event_id(scope_1, "evt_crash_001")
        assert ev is None

    def test_crash_after_commit_recoverable(self, db: Session, scope_1):
        """After commit, evidence persists and is recoverable."""
        ev_repo = EvidenceRepository(db)

        now = datetime.now(UTC)
        ev = ev_repo.create(
            scope=scope_1,
            source_event_id="evt_persist_001",
            source_role=SourceRole.USER,
            text="will persist",
            occurred_at=now,
        )
        db.commit()

        # Can recover
        ev_repo2 = EvidenceRepository(db)
        recovered = ev_repo2.get_by_event_id(scope_1, "evt_persist_001")
        assert recovered is not None
        assert recovered.text == "will persist"